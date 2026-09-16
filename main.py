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
from .core.sync import sync_data


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
    "0.6.0",
)
class WuwaEchoPlugin(Star):
    CROSS_MESSAGE_TTL = 300  # 跨消息缓存图片/待办的有效期(秒)
    REPEAT_GUARD_TTL = 90  # 同一(图,角色,模式)请求的防重复窗口(秒)

    # 强意图: 单独出现即可触发评分
    STRONG_INTENT_KEYWORDS = (
        "评分", "打分", "评级", "鉴定",
        "词条", "品质", "声骸", "声骇", "声海",
        "echo", "Echo", "ECHO",
    )
    # 弱意图: 日常聊天常用词,需要同时有图或角色名才触发,避免私聊误伤
    WEAK_INTENT_KEYWORDS = ("几分", "多少分", "怎么样", "好不好", "强不强")
    INTENT_KEYWORDS = STRONG_INTENT_KEYWORDS + WEAK_INTENT_KEYWORDS
    SET_MODE_KEYWORDS = ("整套", "一套", "全部", "5件", "5 件", "总评", "全套")

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = Path(__file__).parent
        self.default_character = str(
            self.config.get("default_character", "") or "generic_crit"
        ).strip()
        self.show_breakdown = bool(self.config.get("show_substat_breakdown", True))
        self.vision_provider_id = str(self.config.get("vision_provider_id", "") or "").strip()
        self.vision_timeout = float(self.config.get("vision_timeout_sec", 45) or 45)
        self.group_require_at = bool(self.config.get("group_require_at", True))
        self.auto_sync = bool(self.config.get("auto_sync", True))
        self.sync_interval = float(self.config.get("sync_interval_hours", 24) or 24) * 3600.0
        self.scorer = EchoScorer(
            self.plugin_dir / "data" / "weights.json",
            default_character=self.default_character,
        )

        # session_key -> (timestamp, image_url) — "先发图后艾特"用
        self._image_cache: Dict[str, Tuple[float, str]] = {}
        # session_key -> PendingRequest — 缺图或缺角色时登记
        self._pending: Dict[str, PendingRequest] = {}
        # (image_url, character, mode) -> 发起时间 — 防同请求并发/连点重复评分
        self._recent_requests: Dict[Tuple[str, str, str], float] = {}
        self._sync_task: Optional[asyncio.Task] = None

    # ====================== 生命周期 ======================

    async def initialize(self):
        """插件加载后启动数据同步循环(拉取上游新角色权重/别名)。"""
        if self.auto_sync:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def terminate(self):
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None

    async def _sync_loop(self):
        # 等插件完全启动、网络栈就绪再开始第一轮
        await asyncio.sleep(10)
        while True:
            try:
                report = await sync_data(self.plugin_dir / "data")
                if report.weights_changed or report.aliases_changed:
                    self.scorer.load()  # 热重载权重与别名,不用重启
                logger.info(f"[wuwa_echo] 上游数据同步: {report.summary()}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[wuwa_echo] 上游数据同步失败(继续用本地快照): {e}")
            await asyncio.sleep(self.sync_interval)

    # ====================== 监听器(唯一入口,不走 LLM) ======================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """所有消息进这里;识别意图+图+角色名,直接评分,不调任何 LLM。

        关键: 一旦判断本插件要处理这条消息,**立即** stop_event() 阻断 LLM 并发跑,
        再去做耗时的 OCR + 评分。

        分支:
          1. 有 pending 且本条消息能推进(补图/补角色名) → 评分
          2. 有意图 + 角色 + 图(当前或缓存) → 直接评分
          3. 有意图但缺东西 → 登记 pending,提示
          4. 纯发图,无意图 → 静默缓存,不响应
          5. 其他(闲聊等) → 完全放行,让 AstrBot 默认逻辑处理

        群聊默认要求 @ bot(或私聊)才响应,防止路人聊天误触发。
        """
        key = self._session_key(event)
        now = time.time()
        image_url = self._extract_image_url(event)
        text = self._message_text(event).strip()
        addressed = self._is_addressed(event)

        # 总是缓存最近图片(群聊"先发图后艾特"依赖这个)
        if image_url:
            self._image_cache[key] = (now, image_url)
        self._prune_caches(now)

        pending = self._pending.get(key)
        strong_intent = self._has_intent(text, self.STRONG_INTENT_KEYWORDS)
        weak_intent = self._has_intent(text, self.WEAK_INTENT_KEYWORDS)
        # 弱意图词(怎么样/好不好…)太日常,必须搭配图或角色名才算评分意图
        character = ""
        if strong_intent or weak_intent or pending is not None:
            character = self._extract_character_from_text(text)
        has_intent = strong_intent or (weak_intent and bool(image_url or character))

        # ====== 先判断本插件是否吃下这条消息(决定要不要 stop_event) ======
        will_handle = False
        if pending is not None:
            # 只在消息真正推进 pending 时拦截;无关闲聊完全放行,不吃消息
            advances = (image_url and not pending.image_url) or (
                character and not pending.character
            )
            if advances:
                will_handle = True
        if not will_handle and has_intent and (addressed or not self.group_require_at):
            will_handle = True
        if not will_handle and image_url and (addressed or not self.group_require_at):
            # 纯图无意图: 私聊/被@ 时静默缓存并拦截;群里没@的图只缓存、不拦截
            will_handle = True

        if will_handle:
            # 立即终止事件传播 + 跳过 LLM 阶段,彻底阻止 LLM 并发跑
            self._stop(event)
            self._mark_no_llm(event)
            await asyncio.sleep(0)

        # ====== 后续慢处理(此时 LLM 已被阻断) ======

        # 分支 1: 有 pending,用本条消息补齐(有新意图时走分支 2,用最新参数)
        if pending is not None and not has_intent:
            if image_url and not pending.image_url:
                pending.image_url = image_url
            if character and not pending.character:
                pending.character = character
            if pending.image_url and pending.character:
                self._pending.pop(key, None)
                await self._send_score(
                    event, key, pending.image_url, pending.character, pending.mode
                )
                return
            if image_url or character:
                # 推进了但还没齐,续期等待
                pending.timestamp = now
            return

        # 分支 2/3: 有意图
        if has_intent:
            mode = "set" if self._is_set_mode(text) else "single"
            img_url, _ = self._find_image(event)
            # 新消息没提角色名时,沿用 pending 里已记住的角色
            # (场景: 先说"评分今汐",再发"[图] 评分")
            if not character and pending is not None and pending.character:
                character = pending.character

            if character and img_url:
                self._pending.pop(key, None)
                await self._send_score(event, key, img_url, character, mode)
                return

            self._pending[key] = PendingRequest(
                timestamp=now,
                image_url=img_url or "",
                character=character,
                mode=mode,
            )
            if not img_url and not character:
                msg = "请发一张声骸截图,并告诉我角色名(如「今汐」「维妈」「风主」)。"
            elif not img_url:
                msg = f"角色已记({character}),发一下声骸截图吧。"
            else:
                msg = "图已收到,再告诉我角色名(如「今汐」「维妈」「风主」)。"
            await event.send(event.plain_result(msg))
            return

        # 分支 4: 纯图无意图 → 静默(已 stop_event)
        return

    # ====================== Helpers ======================

    def _stop(self, event: AstrMessageEvent) -> None:
        """阻止事件继续传播(其他插件/LLM 不再处理这条消息)。"""
        fn = getattr(event, "stop_event", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    def _mark_no_llm(self, event: AstrMessageEvent) -> None:
        """跳过 AstrBot process_stage 的默认 LLM 调用。

        官方 API 是 should_call_llm(True);老版本没有该方法时退回直接设标志。
        再补设 _has_send_oper 双保险,兼容 v4.24.2 实测的并发 LLM 行为。
        """
        should = getattr(event, "should_call_llm", None)
        if callable(should):
            try:
                should(True)
            except Exception:
                pass
        for attr in ("_has_send_oper", "call_llm"):
            try:
                setattr(event, attr, True)
            except Exception:
                pass

    def _is_addressed(self, event: AstrMessageEvent) -> bool:
        """私聊恒为 True;群聊要求被 @ 或带唤醒前缀。旧版本无该属性时放行。"""
        try:
            if event.is_private_chat():
                return True
        except Exception:
            pass
        return bool(getattr(event, "is_at_or_wake_command", True))

    def _has_intent(self, text: str, keywords: Tuple[str, ...]) -> bool:
        if not text:
            return False
        return any(kw in text for kw in keywords)

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
        stale_req = [
            k
            for k, ts in self._recent_requests.items()
            if now - ts > self.REPEAT_GUARD_TTL
        ]
        for k in stale_req:
            self._recent_requests.pop(k, None)

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
        raw = await vision_recognize(
            provider, image_url, SINGLE_ECHO_PROMPT_ZH, timeout=self.vision_timeout
        )
        echo = parse_single(raw)
        return self.scorer.score_echo(echo, character)

    async def _score_set_from_image(self, image_url: str, character: str) -> SetScoreResult:
        provider = self._get_vision_provider()
        raw = await vision_recognize(
            provider, image_url, SET_ECHO_PROMPT_ZH, timeout=self.vision_timeout
        )
        echo_set = parse_set(raw)
        return self.scorer.score_set(echo_set, character)

    async def _send_score(
        self,
        event: AstrMessageEvent,
        session_key: str,
        image_url: str,
        canonical: str,
        mode: str,
    ) -> None:
        req_key = (image_url, canonical, mode)
        now = time.time()
        last = self._recent_requests.get(req_key)
        if last is not None and now - last <= self.REPEAT_GUARD_TTL:
            # 同一张图同角色的评分已在进行或刚完成,忽略重复触发
            return
        self._recent_requests[req_key] = now
        try:
            if mode == "set":
                result = await self._score_set_from_image(image_url, canonical)
                text = self._format_set_result(result, canonical)
            else:
                result = await self._score_single_from_image(image_url, canonical)
                text = self._format_single_result(result, canonical)
            await event.send(event.plain_result(text))
        except asyncio.TimeoutError:
            logger.warning(f"声骸识别超时({self.vision_timeout}s): {session_key}")
            await event.send(
                event.plain_result(
                    f"识别超时了(超过 {int(self.vision_timeout)} 秒),请稍后再试,或发一张更清晰的截图。"
                )
            )
        except ValueError as e:
            # 校验类错误(副词条不足/解析失败等)本身就是给用户看的中文提示
            logger.info(f"声骸评分校验失败: {e}")
            await event.send(event.plain_result(str(e)))
        except Exception as e:
            logger.exception("声骸评分失败")
            await event.send(
                event.plain_result(
                    f"评分出错了({type(e).__name__}),请稍后再试。"
                    "如果反复失败,检查 vision_provider_id 是否指向多模态模型。"
                )
            )
            # 失败不占用防重复窗口,允许立刻重试
            self._recent_requests.pop(req_key, None)

    def _format_single_result(self, r: ScoreResult, character: str) -> str:
        lines = [
            f"【{character}】 {r.rank}  {r.score:g}/50",
            f"  词条种类 {r.quality_score:g}/25  词条数值 {r.value_score:g}/25",
        ]
        if self.show_breakdown and r.breakdown:
            lines.append("")
            for b in r.breakdown:
                if b.weight == 0:
                    continue
                mark = "★" * min(int(b.weight), 3)
                lines.append(f"  {mark} {b.name} {b.value:g}")
        return "\n".join(lines)

    def _format_set_result(self, r: SetScoreResult, character: str) -> str:
        lines = [
            f"【{character} · 整套】 {r.rank}  总 {r.total_score:g}/250  均 {r.average_score:g}/50",
            "",
        ]
        for idx, item in enumerate(r.items, 1):
            tag = "  ← 短板" if idx - 1 == r.weakest_index else ""
            lines.append(f"  #{idx}  {item.score:g}  {item.rank}{tag}")
        if len(r.items) < 5:
            lines.append(f"  (只识别到 {len(r.items)} 件,完整一套是 5 件)")
        if r.comment:
            lines.append("")
            lines.append(r.comment)
        return "\n".join(lines)
