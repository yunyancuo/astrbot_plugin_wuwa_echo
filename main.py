from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register

from .core.models import ScoreResult, SetScoreResult
from .core.ocr import (
    SET_ECHO_PROMPT_ZH,
    SINGLE_ECHO_PROMPT_ZH,
    parse_set,
    parse_single,
    vision_recognize,
)
from .core.scorer import EchoScorer


@dataclass
class PendingRequest:
    timestamp: float
    image_url: str = ""
    character: str = ""  # 已解析的规范名,空表示尚未拿到
    mode: str = "single"


@register(
    "astrbot_plugin_wuwa_echo",
    "yunyancuo",
    "鸣潮声骸评分插件 — 纯监听器实现,不走 LLM,秒回",
    "0.4.0",
)
class WuwaEchoPlugin(Star):
    CROSS_MESSAGE_TTL = 300  # 跨消息缓存图片/待办的有效期(秒)

    # 触发评分的意图关键词,任一命中即视为"用户想评分"
    INTENT_KEYWORDS = (
        "评分", "打分", "评级", "几分", "多少分", "鉴定",
        "词条", "品质", "怎么样", "好不好", "强不强",
        "echo", "Echo", "ECHO",
        "声骸", "声骇", "声海",
    )
    SET_MODE_KEYWORDS = ("整套", "一套", "全部", "5件", "5 件", "总评", "全套")

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = Path(__file__).parent
        self.scorer = EchoScorer(self.plugin_dir / "data" / "weights.json")
        self.default_character = self.config.get("default_character", "generic_crit")
        self.show_breakdown = bool(self.config.get("show_substat_breakdown", True))
        self.vision_provider_id = str(self.config.get("vision_provider_id", "") or "").strip()

        # session_key -> (timestamp, image_url) — "先发图后艾特"用
        self._image_cache: Dict[str, Tuple[float, str]] = {}
        # session_key -> PendingRequest — 缺图或缺角色时登记
        self._pending: Dict[str, PendingRequest] = {}

    # ====================== 监听器(唯一入口,不走 LLM) ======================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """所有消息进这里;识别意图+图+角色名,直接评分,不调任何 LLM。

        关键: 一旦判断本插件要处理这条消息,**立即** stop_event() 阻断 LLM 并发跑,
        再去做耗时的 OCR + 评分。

        分支:
          1. 有 pending 且能补齐 → 评分
          2. 有意图 + 角色 + 图(当前或缓存) → 直接评分
          3. 有意图但缺东西 → 登记 pending,提示
          4. 纯发图,无意图 → 静默缓存,不响应
          5. 其他(闲聊等) → 透传,让 AstrBot 默认逻辑处理
        """
        key = self._session_key(event)
        now = time.time()
        image_url = self._extract_image_url(event)
        text = self._message_text(event).strip()
        has_intent = self._has_scoring_intent(event)

        # 总是缓存最近图片
        if image_url:
            self._image_cache[key] = (now, image_url)
        self._prune_caches(now)

        pending = self._pending.get(key)

        # ====== 先判断本插件是否吃下这条消息(决定要不要 stop_event) ======
        will_handle = False
        if pending is not None:
            will_handle = True
        elif has_intent:
            will_handle = True
        elif image_url:
            # 纯图无意图: 静默缓存
            will_handle = True

        if will_handle:
            # 立即终止事件传播 + 设两个 AstrBot 内部标志彻底阻止 LLM 阶段触发
            # 仅 stop_event 不够,LLM agent 还会并发跑(实测在 v4.24.2)
            self._stop(event)
            self._mark_no_llm(event)
            await asyncio.sleep(0)

        # ====== 后续慢处理(此时 LLM 已被阻断) ======

        # 分支 1: 有 pending,尝试补齐
        if pending is not None:
            if image_url and not pending.image_url:
                pending.image_url = image_url
            if not pending.character and text and not has_intent:
                resolved = self._resolve_canonical(text)
                if resolved:
                    pending.character = resolved
            if pending.image_url and pending.character:
                self._pending.pop(key, None)
                await self._send_score(
                    event, pending.image_url, pending.character, pending.mode
                )
                return
            pending.timestamp = now
            self._pending[key] = pending
            if not has_intent:
                return
            # has_intent 时往下走,允许覆盖 pending

        # 分支 2/3: 有意图
        if has_intent:
            canonical = self._extract_character_from_text(text)
            img_url, _ = self._find_image(event)
            mode = "set" if self._is_set_mode(text) else "single"

            if canonical and img_url:
                self._pending.pop(key, None)
                await self._send_score(event, img_url, canonical, mode)
                return

            self._pending[key] = PendingRequest(
                timestamp=now,
                image_url=img_url or "",
                character=canonical,
                mode=mode,
            )
            if not img_url and not canonical:
                msg = "请发声骸截图并告诉我角色名(如「今汐」「维妈」「风主」)。"
            elif not img_url:
                msg = f"角色已记({canonical}),请发声骸截图。"
            else:
                msg = "图已收到,请告诉我角色名(如「今汐」「维妈」「风主」)。"
            await event.send(event.plain_result(msg))
            return

        # 分支 4: 纯图无意图 → 静默(已 stop_event)
        if image_url:
            return

        # 分支 5: 其他文字 → 透传
        return

    # ====================== Helpers ======================

    def _stop(self, event: AstrMessageEvent) -> None:
        """阻止事件继续传播(LLM/其他插件不会再处理这条消息)。"""
        for name in ("stop_event", "stop", "halt"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:
                    pass

    def _mark_no_llm(self, event: AstrMessageEvent) -> None:
        """设置 AstrBot 内部标志,跳过 process_stage 的 LLM 调用。

        AstrBot v4.24.2 的 process_stage/stage.py 在判断是否调 LLM 时检查:
          - event._has_send_oper == True → 跳
          - event.call_llm == True → 跳(表示"已调过 LLM")
        listener 走的路径不自动设这两个标志,LLM agent 会并发跑出 14-20s 幽灵 tail。
        手动设上以彻底阻断。
        """
        for attr in ("_has_send_oper", "call_llm"):
            try:
                setattr(event, attr, True)
            except Exception:
                pass

    def _resolve_canonical(self, raw: str) -> str:
        """把任意输入解析为规范名,失败返回空串。"""
        query = (raw or "").strip()
        if not query:
            return ""
        result = self.scorer.resolve(query)
        return result.canonical if result else ""

    def _extract_character_from_text(self, text: str) -> str:
        """从消息文本中抠出第一个能匹配上的角色规范名,失败返回空串。"""
        if not text:
            return ""
        cleaned = text.lower()
        for kw in self.INTENT_KEYWORDS:
            cleaned = cleaned.replace(kw.lower(), " ")
        for kw in self.SET_MODE_KEYWORDS:
            cleaned = cleaned.replace(kw.lower(), " ")
        if not cleaned.strip():
            return ""

        resolver = self.scorer.resolver
        # 优先匹配规范名(更准),再匹配长度 >= 2 的别名
        for canonical in resolver.canonical_names():
            if canonical.lower() in cleaned:
                return canonical
        for canonical in resolver.canonical_names():
            for alias in resolver.aliases_of(canonical):
                if len(alias) >= 2 and alias.lower() in cleaned:
                    return canonical
        return ""

    def _is_set_mode(self, text: str) -> bool:
        return any(kw in text for kw in self.SET_MODE_KEYWORDS)

    def _extract_image_url(self, event: AstrMessageEvent) -> Optional[str]:
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                return getattr(comp, "url", None) or getattr(comp, "file", None)
        return None

    def _session_key(self, event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            val = getattr(event, attr, None)
            if val:
                return str(val)
        sender = getattr(event, "get_sender_id", lambda: None)()
        group = getattr(event, "get_group_id", lambda: None)()
        return f"{group or ''}:{sender or ''}"

    def _find_image(
        self, event: AstrMessageEvent
    ) -> Tuple[Optional[str], Optional[str]]:
        """返回 (url, source); source: 'current' | 'cache' | None。"""
        url = self._extract_image_url(event)
        if url:
            return url, "current"
        key = self._session_key(event)
        cached = self._image_cache.get(key)
        if cached and (time.time() - cached[0]) <= self.CROSS_MESSAGE_TTL:
            return cached[1], "cache"
        return None, None

    def _prune_caches(self, now: float) -> None:
        ttl = self.CROSS_MESSAGE_TTL
        stale_img = [k for k, v in self._image_cache.items() if now - v[0] > ttl]
        for k in stale_img:
            self._image_cache.pop(k, None)
        stale_pending = [
            k for k, v in self._pending.items() if now - v.timestamp > ttl
        ]
        for k in stale_pending:
            self._pending.pop(k, None)

    def _message_text(self, event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", "") or ""
        if text:
            return text
        parts = []
        for comp in event.message_obj.message:
            t = getattr(comp, "text", None)
            if isinstance(t, str):
                parts.append(t)
        return " ".join(parts)

    def _has_scoring_intent(self, event: AstrMessageEvent) -> bool:
        text = self._message_text(event)
        if not text:
            return False
        return any(kw in text for kw in self.INTENT_KEYWORDS)

    # ====================== Vision OCR ======================

    def _get_vision_provider(self):
        if self.vision_provider_id:
            try:
                p = self.context.get_provider_by_id(self.vision_provider_id)
                if p:
                    return p
                logger.warning(
                    f"vision_provider_id='{self.vision_provider_id}' 查无此 provider,回落到当前 provider"
                )
            except Exception as e:
                logger.warning(
                    f"加载 vision provider '{self.vision_provider_id}' 失败: {e},回落到当前 provider"
                )
        p = self.context.get_using_provider()
        if not p:
            raise RuntimeError(
                "没有可用的视觉 provider。请在 AstrBot WebUI 配多模态模型,"
                "并在插件配置 vision_provider_id 指向它。"
            )
        return p

    async def _score_single_from_image(self, image_url: str, character: str) -> ScoreResult:
        provider = self._get_vision_provider()
        raw = await vision_recognize(provider, image_url, SINGLE_ECHO_PROMPT_ZH)
        echo = parse_single(raw)
        return self.scorer.score_echo(echo, character)

    async def _score_set_from_image(self, image_url: str, character: str) -> SetScoreResult:
        provider = self._get_vision_provider()
        raw = await vision_recognize(provider, image_url, SET_ECHO_PROMPT_ZH)
        echo_set = parse_set(raw)
        return self.scorer.score_set(echo_set, character)

    async def _send_score(
        self,
        event: AstrMessageEvent,
        image_url: str,
        canonical: str,
        mode: str,
    ) -> None:
        try:
            if mode == "set":
                result = await self._score_set_from_image(image_url, canonical)
                text = self._format_set_result(result, canonical)
            else:
                result = await self._score_single_from_image(image_url, canonical)
                text = self._format_single_result(result, canonical)
            await event.send(event.plain_result(text))
        except Exception as e:
            logger.exception("声骸评分失败")
            await event.send(event.plain_result(f"评分失败: {e}"))

    def _format_single_result(self, r: ScoreResult, character: str) -> str:
        lines = [
            f"【{character}】 {r.rank}  {r.score}/50",
            f"  词条种类 {r.quality_score}/25  词条数值 {r.value_score}/25",
        ]
        if self.show_breakdown and r.breakdown:
            lines.append("")
            for b in r.breakdown:
                if b.weight == 0:
                    continue
                mark = "★" * min(int(b.weight), 3)
                lines.append(f"  {mark} {b.name} {b.value}")
        return "\n".join(lines)

    def _format_set_result(self, r: SetScoreResult, character: str) -> str:
        lines = [
            f"【{character} · 整套】 {r.rank}  {r.total_score}/250  均 {r.average_score}/50",
            "",
        ]
        for idx, item in enumerate(r.items, 1):
            tag = "  (短板)" if idx - 1 == r.weakest_index else ""
            lines.append(f"  #{idx}  {item.score}  {item.rank}{tag}")
        return "\n".join(lines)
