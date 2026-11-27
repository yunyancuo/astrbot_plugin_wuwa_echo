"""鸣潮声骸评分 AstrBot 插件主入口。

纯事件监听器架构：识别群聊 @ 门控与评分意图后，立即阻断默认 LLM 阶段并发，
再执行多模态 OCR 识别与双指标评分，实现低延迟秒回。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    redact_sensitive,
    vision_recognize,
)
from .core.scorer import EchoScorer
from .core.sync import sync_data


@dataclass
class PendingRequest:
    """跨消息补齐状态：记录等待补图或补角色名的半成品评分请求。"""

    timestamp: float
    image_url: str = ""
    character: str = ""  # 已解析的规范名，空串表示尚未拿到
    mode: str = "single"


def _parse_config_float(
    raw: Any,
    *,
    default: float,
    min_value: float,
    field_name: str,
) -> float:
    """安全解析配置中的正浮点参数并做下限防护，防止异常配置引发崩溃或死循环。"""
    if raw is None or raw == "" or isinstance(raw, bool):
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"[wuwa_echo] 配置项 {field_name}={raw!r} 非法，已回落默认值 {default}"
        )
        return default
    if not math.isfinite(val) or val <= 0:
        logger.warning(
            f"[wuwa_echo] 配置项 {field_name}={raw!r} 必须为正数，已回落默认值 {default}"
        )
        return default
    if val < min_value:
        logger.warning(
            f"[wuwa_echo] 配置项 {field_name}={val} 低于安全下限 {min_value}，已自动调整"
        )
        return min_value
    return val


@register(
    "astrbot_plugin_wuwa_echo",
    "yunyancuo",
    "鸣潮声骸评分插件 — 纯监听器实现,不走 LLM,秒回",
    "0.6.0",
)
class WuwaEchoPlugin(Star):
    """鸣潮声骸评分插件主类。"""

    CROSS_MESSAGE_TTL = 300  # 跨消息缓存图片/待办的有效期(秒)
    REPEAT_GUARD_TTL = 90  # 同一(图,角色,模式)请求的防重复窗口(秒)
    MAX_CACHE_ENTRIES = 1000  # 单类内存缓存条目上限，防长期运行膨胀
    MIN_VISION_TIMEOUT_SEC = 5.0  # 视觉识别最小超时保护(秒)
    MIN_SYNC_INTERVAL_HOURS = 1.0  # 上游同步最小间隔保护(小时)

    # 强意图: 单独出现即可触发评分
    STRONG_INTENT_KEYWORDS = (
        "评分",
        "打分",
        "评级",
        "鉴定",
        "词条",
        "品质",
        "声骸",
        "声骇",
        "声海",
        "echo",
        "Echo",
        "ECHO",
    )
    # 弱意图: 日常聊天常用词，需要同时有图或角色名才触发，避免私聊误伤
    WEAK_INTENT_KEYWORDS = ("几分", "多少分", "怎么样", "好不好", "强不强")
    INTENT_KEYWORDS = STRONG_INTENT_KEYWORDS + WEAK_INTENT_KEYWORDS
    SET_MODE_KEYWORDS = ("整套", "一套", "全部", "5件", "5 件", "总评", "全套")

    # 预按长度降序排好的清洗关键词（小写去重），确保先清洗长词再清洗短词
    _CLEAN_KEYWORDS = tuple(
        sorted(
            {kw.lower() for kw in (INTENT_KEYWORDS + SET_MODE_KEYWORDS)},
            key=len,
            reverse=True,
        )
    )

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = Path(__file__).parent
        self.default_character = str(
            self.config.get("default_character", "") or "generic_crit"
        ).strip()
        self.show_breakdown = bool(self.config.get("show_substat_breakdown", True))
        self.vision_provider_id = str(
            self.config.get("vision_provider_id", "") or ""
        ).strip()
        self.vision_timeout = _parse_config_float(
            self.config.get("vision_timeout_sec", 45),
            default=45.0,
            min_value=self.MIN_VISION_TIMEOUT_SEC,
            field_name="vision_timeout_sec",
        )
        self.group_require_at = bool(self.config.get("group_require_at", True))
        self.auto_sync = bool(self.config.get("auto_sync", True))
        sync_hours = _parse_config_float(
            self.config.get("sync_interval_hours", 24),
            default=24.0,
            min_value=self.MIN_SYNC_INTERVAL_HOURS,
            field_name="sync_interval_hours",
        )
        self.sync_interval = sync_hours * 3600.0
        self.scorer = EchoScorer(
            self.plugin_dir / "data" / "weights.json",
            default_character=self.default_character,
        )

        # session_key -> (timestamp, image_url) — "先发图后艾特"用
        self._image_cache: dict[str, tuple[float, str]] = {}
        # session_key -> PendingRequest — 缺图或缺角色时登记
        self._pending: dict[str, PendingRequest] = {}
        # (image_url, character, mode) -> 发起时间 — 防同请求并发/连点重复评分
        self._recent_requests: dict[tuple[str, str, str], float] = {}
        self._sync_task: asyncio.Task[None] | None = None

    # ====================== 生命周期 ======================

    async def initialize(self) -> None:
        """插件加载后启动数据同步循环（拉取上游新角色权重/别名）。"""
        if self.auto_sync and (self._sync_task is None or self._sync_task.done()):
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def terminate(self) -> None:
        """插件卸载或重载时优雅取消后台同步任务并释放缓存。"""
        task = self._sync_task
        self._sync_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[wuwa_echo] 停止同步任务时出现异常: {e}")
        self._image_cache.clear()
        self._pending.clear()
        self._recent_requests.clear()

    async def _sync_loop(self) -> None:
        """后台定时同步上游角色权重与别名，变更后原子热重载。"""
        # 等插件完全启动、网络栈就绪再开始第一轮
        await asyncio.sleep(10)
        while True:
            try:
                report = await sync_data(self.plugin_dir / "data")
                if report.weights_changed or report.aliases_changed:
                    self.scorer.load()  # 热重载权重与别名，不用重启
                logger.info(f"[wuwa_echo] 上游数据同步: {report.summary()}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[wuwa_echo] 上游数据同步失败(继续用本地快照): {e}")
            await asyncio.sleep(self.sync_interval)

    # ====================== 监听器(唯一入口,不走 LLM) ======================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """所有消息进这里；识别意图+图+角色名，直接评分，不调任何 LLM。

        关键: 一旦判断本插件要处理这条消息，**立即** stop_event() 阻断 LLM 并发跑，
        再去做耗时的 OCR + 评分。

        分支:
          1. 有 pending 且本条消息能推进(补图/补角色名) → 评分或引导补齐
          2. 有意图 + 角色 + 图(当前或缓存) → 直接评分
          3. 有意图但缺东西 → 登记 pending，提示
          4. 纯发图，无意图 → 静默缓存，不响应
          5. 其他(闲聊/群聊未@) → 完全放行，让 AstrBot 默认逻辑处理

        群聊默认要求 @ bot（或私聊）才响应，防止路人聊天误触发。
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
        # 弱意图词(怎么样/好不好…)太日常，必须搭配图或角色名才算评分意图
        character = ""
        if strong_intent or weak_intent or pending is not None:
            character = self._extract_character_from_text(text)
        has_intent = strong_intent or (weak_intent and bool(image_url or character))
        can_trigger_intent = addressed or not self.group_require_at

        # ====== 先判断本插件是否吃下这条消息(决定要不要 stop_event) ======
        will_handle = False
        if pending is not None:
            # 只在消息真正推进 pending 时拦截；无关闲聊完全放行，不吃消息
            advances = (bool(image_url) and not pending.image_url) or (
                bool(character) and not pending.character
            )
            if advances:
                will_handle = True
        if not will_handle and has_intent and can_trigger_intent:
            will_handle = True
        if not will_handle and image_url and can_trigger_intent:
            # 纯图无意图: 私聊/被@ 时静默缓存并拦截；群里没@的图只缓存、不拦截
            will_handle = True

        if not will_handle:
            # 未命中门控或无关闲聊：直接放行，绝不穿透到后续评分/反问分支
            return

        # 立即终止事件传播 + 跳过 LLM 阶段，彻底阻止 LLM 并发跑
        self._stop(event)
        self._mark_no_llm(event)
        await asyncio.sleep(0)

        # ====== 后续慢处理(此时 LLM 已被阻断) ======

        # 分支 1: 有 pending，用本条消息补齐(当有且仅当满足触发门控的新意图时走分支 2)
        if pending is not None and not (has_intent and can_trigger_intent):
            advanced = False
            if image_url and not pending.image_url:
                pending.image_url = image_url
                advanced = True
            if character and not pending.character:
                pending.character = character
                advanced = True
            if pending.image_url and pending.character:
                self._pending.pop(key, None)
                await self._send_score(
                    event, key, pending.image_url, pending.character, pending.mode
                )
                return
            if advanced:
                # 推进了一项但仍缺另一项，续期并提示下一步
                pending.timestamp = now
                if not pending.image_url:
                    msg = f"角色已记({pending.character}),发一下声骸截图吧。"
                else:
                    msg = "图已收到,再告诉我角色名(如「今汐」「维妈」「风主」)。"
                await event.send(event.plain_result(msg))
            return

        # 分支 2/3: 有意图且通过 @ 门控
        if has_intent and can_trigger_intent:
            mode = "set" if self._is_set_mode(text) else "single"
            img_url, _ = self._find_image(event)
            # 新消息没提角色名时，沿用 pending 里已记住的角色
            # (场景: 先说"评分今汐"，再发"[图] 评分")
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
        """阻止事件继续传播（其他插件/LLM 不再处理这条消息）。"""
        fn = getattr(event, "stop_event", None)
        if callable(fn):
            with contextlib.suppress(Exception):
                fn()

    def _mark_no_llm(self, event: AstrMessageEvent) -> None:
        """跳过 AstrBot process_stage 的默认 LLM 调用。

        官方 API 是 should_call_llm(True)；老版本没有该方法时退回直接设标志。
        再补设 _has_send_oper 双保险，兼容 v4.24.2 实测的并发 LLM 行为。
        """
        should = getattr(event, "should_call_llm", None)
        if callable(should):
            with contextlib.suppress(Exception):
                should(True)
        for attr in ("_has_send_oper", "call_llm"):
            with contextlib.suppress(Exception):
                setattr(event, attr, True)

    def _is_addressed(self, event: AstrMessageEvent) -> bool:
        """私聊恒为 True；群聊要求被 @ 或带唤醒前缀。旧版本无该属性时放行。"""
        try:
            if event.is_private_chat():
                return True
        except Exception:  # noqa: BLE001
            pass
        return bool(getattr(event, "is_at_or_wake_command", True))

    def _has_intent(self, text: str, keywords: tuple[str, ...]) -> bool:
        """大小写不敏感检查消息是否包含任一意图关键词。"""
        if not text:
            return False
        lowered = text.lower()
        return any(kw.lower() in lowered for kw in keywords)

    def _resolve_canonical(self, raw: str) -> str:
        """把任意输入解析为规范名，失败返回空串。"""
        query = (raw or "").strip()
        if not query:
            return ""
        result = self.scorer.resolve(query)
        return result.canonical if result else ""

    def _extract_character_from_text(self, text: str) -> str:
        """从消息文本中提取最优先匹配的角色规范名，失败返回空串。"""
        if not text:
            return ""
        cleaned = text.lower()
        for kw in self._CLEAN_KEYWORDS:
            cleaned = cleaned.replace(kw, " ")
        if not cleaned.strip():
            return ""
        return self.scorer.resolver.extract_from_text(cleaned)

    def _is_set_mode(self, text: str) -> bool:
        """判断用户是否请求整套声骸评分。"""
        if not text:
            return False
        lowered = text.lower()
        return any(kw.lower() in lowered for kw in self.SET_MODE_KEYWORDS)

    def _extract_image_url(self, event: AstrMessageEvent) -> str | None:
        """从事件消息链中提取首张图片的 URL 或文件路径。"""
        msg_obj = getattr(event, "message_obj", None)
        components = getattr(msg_obj, "message", None)
        if not components:
            return None
        for comp in components:
            if isinstance(comp, Image):
                raw = getattr(comp, "url", None) or getattr(comp, "file", None)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        return None

    def _session_key(self, event: AstrMessageEvent) -> str:
        """构造用于跨消息缓存与待办追踪的会话键。"""
        for attr in ("unified_msg_origin", "session_id"):
            val = getattr(event, attr, None)
            if val:
                return str(val)
        sender_fn = getattr(event, "get_sender_id", None)
        group_fn = getattr(event, "get_group_id", None)
        sender = sender_fn() if callable(sender_fn) else None
        group = group_fn() if callable(group_fn) else None
        return f"{group or ''}:{sender or ''}"

    def _find_image(
        self, event: AstrMessageEvent
    ) -> tuple[str | None, str | None]:
        """返回 (url, source)；source 为 'current' | 'cache' | None。"""
        url = self._extract_image_url(event)
        if url:
            return url, "current"
        key = self._session_key(event)
        cached = self._image_cache.get(key)
        if cached and (time.time() - cached[0]) <= self.CROSS_MESSAGE_TTL:
            return cached[1], "cache"
        return None, None

    def _prune_caches(self, now: float) -> None:
        """清理过期缓存条目，并在超出容量上限时淘汰最旧条目。"""
        ttl = self.CROSS_MESSAGE_TTL
        stale_img = [k for k, v in self._image_cache.items() if now - v[0] > ttl]
        for k in stale_img:
            self._image_cache.pop(k, None)
        if len(self._image_cache) > self.MAX_CACHE_ENTRIES:
            overflow = len(self._image_cache) - self.MAX_CACHE_ENTRIES
            oldest = sorted(self._image_cache.items(), key=lambda kv: kv[1][0])[:overflow]
            for k, _ in oldest:
                self._image_cache.pop(k, None)

        stale_pending = [
            k for k, v in self._pending.items() if now - v.timestamp > ttl
        ]
        for k in stale_pending:
            self._pending.pop(k, None)
        if len(self._pending) > self.MAX_CACHE_ENTRIES:
            overflow = len(self._pending) - self.MAX_CACHE_ENTRIES
            oldest_p = sorted(self._pending.items(), key=lambda kv: kv[1].timestamp)[:overflow]
            for k, _ in oldest_p:
                self._pending.pop(k, None)

        stale_req = [
            k
            for k, ts in self._recent_requests.items()
            if now - ts > self.REPEAT_GUARD_TTL
        ]
        for k in stale_req:
            self._recent_requests.pop(k, None)
        if len(self._recent_requests) > self.MAX_CACHE_ENTRIES:
            overflow = len(self._recent_requests) - self.MAX_CACHE_ENTRIES
            oldest_r = sorted(self._recent_requests.items(), key=lambda kv: kv[1])[:overflow]
            for k, _ in oldest_r:
                self._recent_requests.pop(k, None)

    def _message_text(self, event: AstrMessageEvent) -> str:
        """提取事件纯文本内容。"""
        text = getattr(event, "message_str", "") or ""
        if isinstance(text, str) and text:
            return text
        msg_obj = getattr(event, "message_obj", None)
        components = getattr(msg_obj, "message", None)
        if not components:
            return ""
        parts = []
        for comp in components:
            t = getattr(comp, "text", None)
            if isinstance(t, str):
                parts.append(t)
        return " ".join(parts)

    # ====================== Vision OCR ======================

    def _get_vision_provider(self) -> Any:
        """获取配置的视觉 provider，未配置或查无此 ID 时回落当前默认 provider。"""
        if self.vision_provider_id:
            try:
                p = self.context.get_provider_by_id(self.vision_provider_id)
                if p:
                    return p
                logger.warning(
                    f"vision_provider_id='{self.vision_provider_id}' 查无此 provider,回落到当前 provider"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"加载 vision provider '{self.vision_provider_id}' 失败: {redact_sensitive(str(e))},回落到当前 provider"
                )
        p = self.context.get_using_provider()
        if not p:
            raise RuntimeError(
                "没有可用的视觉 provider。请在 AstrBot WebUI 配多模态模型,"
                "并在插件配置 vision_provider_id 指向它。"
            )
        return p

    async def _score_single_from_image(self, image_url: str, character: str) -> ScoreResult:
        """识别并评分单件声骸截图。"""
        provider = self._get_vision_provider()
        raw = await vision_recognize(
            provider, image_url, SINGLE_ECHO_PROMPT_ZH, timeout=self.vision_timeout
        )
        echo = parse_single(raw)
        return self.scorer.score_echo(echo, character)

    async def _score_set_from_image(self, image_url: str, character: str) -> SetScoreResult:
        """识别并评分整套声骸截图。"""
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
        """执行防重保护、OCR 识别评分并回复消息；任一失败均释放防重窗口以允许立即重试。"""
        req_key = (image_url, canonical, mode)
        now = time.time()
        last = self._recent_requests.get(req_key)
        if last is not None and now - last <= self.REPEAT_GUARD_TTL:
            # 同一张图同角色的评分已在进行或刚完成，忽略重复触发
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
        except asyncio.CancelledError:
            self._recent_requests.pop(req_key, None)
            raise
        except asyncio.TimeoutError:
            # 超时不占用防重复窗口，允许用户立即重试
            self._recent_requests.pop(req_key, None)
            logger.warning(f"声骸识别超时({self.vision_timeout}s): {session_key}")
            await event.send(
                event.plain_result(
                    f"识别超时了(超过 {int(self.vision_timeout)} 秒),请稍后再试,或发一张更清晰的截图。"
                )
            )
        except ValueError as e:
            # 校验类错误(副词条不足/解析失败等)释放防重复窗口，允许立刻重试
            self._recent_requests.pop(req_key, None)
            safe_msg = redact_sensitive(str(e))
            logger.info(f"声骸评分校验失败: {safe_msg}")
            await event.send(event.plain_result(safe_msg))
        except Exception as e:  # noqa: BLE001
            self._recent_requests.pop(req_key, None)
            logger.exception("声骸评分失败")
            await event.send(
                event.plain_result(
                    f"评分出错了({type(e).__name__}),请稍后再试。"
                    "如果反复失败,检查 vision_provider_id 是否指向多模态模型。"
                )
            )

    def _format_single_result(self, r: ScoreResult, character: str) -> str:
        """格式化单件声骸评分回复文本。"""
        lines = [
            f"【{character}】 {r.rank}  {r.score:g}/50",
            f"  词条种类 {r.quality_score:g}/25  词条数值 {r.value_score:g}/25",
        ]
        if self.show_breakdown and r.breakdown:
            lines.append("")
            for b in r.breakdown:
                if b.weight <= 0:
                    continue
                stars = max(1, min(round(b.weight), 3))
                mark = "★" * stars
                lines.append(f"  {mark} {b.name} {b.value:g}")
        return "\n".join(lines)

    def _format_set_result(self, r: SetScoreResult, character: str) -> str:
        """格式化整套声骸评分回复文本。"""
        lines = [
            f"【{character} · 整套】 {r.rank}  总 {r.total_score:g}/250  均 {r.average_score:g}/50",
            "",
        ]
        for idx, item in enumerate(r.items, 1):
            tag = "  ← 短板" if idx - 1 == r.weakest_index else ""
            lines.append(f"  #{idx}  {item.score:g}  {item.rank}{tag}")
        incomplete_note = len(r.items) < 5
        if incomplete_note:
            lines.append(f"  (只识别到 {len(r.items)} 件,完整一套是 5 件)")
        # 避免缺件时重复打印两条含义相同的提示
        if r.comment and not (incomplete_note and "完整一套" in r.comment):
            lines.append("")
            lines.append(r.comment)
        return "\n".join(lines)
