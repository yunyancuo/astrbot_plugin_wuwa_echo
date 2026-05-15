from __future__ import annotations

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
    character: str = ""  # 已解析的规范名(canonical),空表示尚未拿到
    mode: str = "single"


@register(
    "astrbot_plugin_wuwa_echo",
    "yunyancuo",
    "鸣潮声骸评分插件 — 静默缓存图片,关键词触发评分,只输出结果",
    "0.3.0",
)
class WuwaEchoPlugin(Star):
    CROSS_MESSAGE_TTL = 300  # 跨消息缓存图片/待办的有效期(秒)

    # 触发评分的意图关键词。任一命中 + (当前或缓存)图片 → 直接评分。
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
        # session_key -> PendingRequest — "先艾特后发图"或缺角色名用
        self._pending: Dict[str, PendingRequest] = {}

    # ===================== 唯一 LLM 工具 =====================

    @filter.llm_tool(name="score_wuwa_echo")
    async def tool_score_echo(
        self,
        event: AstrMessageEvent,
        character: str = "",
        mode: str = "single",
    ):
        """评分鸣潮(Wuthering Waves)声骸截图。

        **严格触发条件**: 用户消息文本必须包含「评分/打分/评级/几分/多少分/鉴定/词条/品质/怎么样/好不好/强不强/echo/声骸/鸣潮」之一。
        以下情况一律不要调用本工具,直接走普通对话:
          - 用户只发图但没说评分意图(可能是其他游戏图、表情包)
          - 闲聊、问候、问其他游戏问题

        **不要在调用本工具前后说任何"好的我来评分""先查一下别名"之类的话**,直接调用即可,工具自己会把结果格式化好。

        Args:
            character(string): 角色名或昵称(如「今汐」「维妈」「verina」「龙女」「风主」)。用户没说就传空字符串。
            mode(string): "single" 单件 / "set" 整套。用户提到"整套/一套/全部/5件/总评"时用 set,否则 single。
        """
        if not self._has_scoring_intent(event):
            return

        key = self._session_key(event)
        now = time.time()
        self._prune_caches(now)

        # 解析角色名(可能解析失败)
        canonical = self._resolve_canonical(character)

        # 找图: 优先当前消息,回落到缓存
        image_url, _ = self._find_image(event)

        # 凑齐了 → 直接评分
        if image_url and canonical:
            self._pending.pop(key, None)
            async for chunk in self._do_score(event, image_url, canonical, mode):
                yield chunk
            # 显式终止 agent loop, 避免 AstrBot 等 60s 才打完成日志
            self._stop(event)
            return

        # 不齐: 登记 pending,等用户补齐
        self._pending[key] = PendingRequest(
            timestamp=now,
            image_url=image_url or "",
            character=canonical,
            mode=mode,
        )
        if not image_url and not canonical:
            yield event.plain_result("请发声骸截图,并告诉我角色名。")
        elif not image_url:
            yield event.plain_result(f"角色已记({canonical}),请发声骸截图。")
        else:
            yield event.plain_result("图已收到,请告诉我角色名(如「今汐」「维妈」「风主」)。")
        self._stop(event)

    # ============== Passive listener: 缓存图片 + 续办 pending ==============

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """监听所有消息: 见图静默缓存,无意图直接拦截 LLM; 有 pending 时凑齐自动评分。"""
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

        # 有 pending → 尝试补齐(图/角色)
        if pending is not None:
            if image_url and not pending.image_url:
                pending.image_url = image_url
            if not pending.character and text and not has_intent:
                # 仅当用户当前消息不是评分意图(避免重复触发 tool)
                resolved = self._resolve_canonical(text)
                if resolved:
                    pending.character = resolved

            # 凑齐了 → 评分,并阻止 LLM 再回一条
            if pending.image_url and pending.character:
                self._pending.pop(key, None)
                try:
                    async for chunk in self._do_score(
                        event, pending.image_url, pending.character, pending.mode
                    ):
                        await event.send(chunk)
                except Exception as e:
                    logger.exception("listener 续办评分失败")
                    await event.send(event.plain_result(f"评分失败: {e}"))
                self._stop(event)
                return

            # 还不齐但有进展: 更新时间戳并维持 pending
            pending.timestamp = now
            self._pending[key] = pending
            return

        # 快速通道: 意图 + 文本里能抠出角色名 + 有图 → 跳过 LLM 直接评分
        # 省 1-2s 的 LLM 决策时间,且彻底避免 agent loop 收尾等待
        if has_intent:
            canonical = self._extract_character_from_text(text)
            img_url, _ = self._find_image(event)
            if canonical and img_url:
                mode = "set" if self._is_set_mode(text) else "single"
                try:
                    async for chunk in self._do_score(
                        event, img_url, canonical, mode
                    ):
                        await event.send(chunk)
                    self._stop(event)
                    return
                except Exception:
                    logger.exception("快速通道评分失败,回退给 LLM tool")
                    # 不 stop_event,让 LLM tool 接管

        # 没 pending: 用户只发图(没意图关键词) → 完全静默,拦截 LLM
        # 用户发了其他文字: 让 LLM 走默认对话流程
        if image_url and not has_intent:
            self._stop(event)
            return

    def _stop(self, event: AstrMessageEvent) -> None:
        """阻止事件继续往下传(LLM/其他插件不会再处理这条消息)。"""
        for name in ("stop_event", "stop", "halt"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:
                    pass

    # ===================== Helpers =====================

    def _resolve_canonical(self, raw: str) -> str:
        """把任意输入(角色名/别名/缩写)解析为规范名,失败返回空串。"""
        query = (raw or "").strip()
        if not query:
            return ""
        result = self.scorer.resolve(query)
        return result.canonical if result else ""

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
        stale_pending = [k for k, v in self._pending.items() if now - v.timestamp > ttl]
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

    def _is_set_mode(self, text: str) -> bool:
        return any(kw in text for kw in self.SET_MODE_KEYWORDS)

    def _extract_character_from_text(self, text: str) -> str:
        """从消息文本中抠出第一个能匹配上的角色规范名。

        - 先剥掉意图关键词避免和角色名子串冲突
        - 优先匹配规范名(更准),再匹配长度 >= 2 的别名
        - 用于快速通道, 失败返回空串(让 LLM 接管)
        """
        if not text:
            return ""
        cleaned = text.lower()
        for kw in self.INTENT_KEYWORDS:
            cleaned = cleaned.replace(kw.lower(), " ")
        if not cleaned.strip():
            return ""

        resolver = self.scorer.resolver
        for canonical in resolver.canonical_names():
            if canonical.lower() in cleaned:
                return canonical
        for canonical in resolver.canonical_names():
            for alias in resolver.aliases_of(canonical):
                if len(alias) >= 2 and alias.lower() in cleaned:
                    return canonical
        return ""

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
                "没有可用的 LLM provider。请在 AstrBot WebUI 配多模态模型,"
                "或在插件配置 vision_provider_id 指向已配置的视觉 provider。"
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

    async def _do_score(
        self,
        event: AstrMessageEvent,
        image_url: str,
        canonical: str,
        mode: str,
    ):
        try:
            if mode == "set":
                result = await self._score_set_from_image(image_url, canonical)
                yield event.plain_result(self._format_set_result(result, canonical))
            else:
                result = await self._score_single_from_image(image_url, canonical)
                yield event.plain_result(self._format_single_result(result, canonical))
        except Exception as e:
            logger.exception("声骸评分失败")
            yield event.plain_result(f"评分失败: {e}")

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
