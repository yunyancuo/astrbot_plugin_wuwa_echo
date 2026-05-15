from __future__ import annotations

import time
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


@register(
    "astrbot_plugin_wuwa_echo",
    "you",
    "鸣潮声骸评分插件 — 自然语言+图片自动识别并打分",
    "0.2.0",
)
class WuwaEchoPlugin(Star):
    CROSS_MESSAGE_TTL = 300  # 跨消息缓存图片/待办的有效期(秒)

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = Path(__file__).parent
        self.scorer = EchoScorer(self.plugin_dir / "data" / "weights.json")
        self.default_character = self.config.get("default_character", "generic_crit")
        self.show_breakdown = bool(self.config.get("show_substat_breakdown", True))
        self.vision_provider_id = str(self.config.get("vision_provider_id", "") or "").strip()

        # session_key -> (timestamp, image_url)  — 用户最近发的图,供"先发图后艾特"复用
        self._image_cache: Dict[str, Tuple[float, str]] = {}
        # session_key -> (timestamp, character, mode)  — 已发评分请求但还差图,供"先艾特后发图"自动续上
        self._pending_requests: Dict[str, Tuple[float, str, str]] = {}

    # 触发本插件评分工具的意图关键词。任一命中 + 附带图片 + @bot 即触发。
    # 故意挑了好打的常用词,避开生僻字「骸」。
    INTENT_KEYWORDS = (
        "评分", "打分", "评级", "几分", "多少分", "鉴定",
        "词条", "品质", "怎么样", "好不好", "强不强",
        "echo", "Echo", "ECHO",
        "声骸", "声骇", "声海",
        "鸣潮", "wuwa", "WUWA",
    )

    # ===================== LLM Tools =====================

    @filter.llm_tool(name="score_wuwa_echo")
    async def tool_score_echo(
        self,
        event: AstrMessageEvent,
        character: str = "",
        mode: str = "single",
    ):
        """评分鸣潮(Wuthering Waves)声骸截图。

        **严格触发条件 (必须全部满足才能调用)**:
          1. 用户消息附带了图片(声骸截图)
          2. 用户消息文本表达了"想要评分"的意图,例如包含「评分/打分/评级/几分/多少分/鉴定/词条/品质/怎么样/好不好/强不强/echo/声骸/鸣潮」中任一关键词
          3. 用户已经 @ 了机器人(由群聊唤醒机制保证,无需你判断)

        以下情况一律不要调用本工具:
          - 只发图没说意图(可能是其他游戏截图、表情包、随手发的图)
          - 只说意图没附图(应让用户发图,不要调用)
          - 闲聊、问候、问其他游戏问题、问鸣潮其他玩法(只评分声骸,不解答攻略)

        Args:
            character(string): 角色名或昵称(如「今汐」「维妈」「verina」「龙女」「风主」)。用户未明确指定角色时,传入空字符串触发反问。
            mode(string): "single" 表示单件声骸评分(用户只发了 1 件、或问"这个声骸怎么样"); "set" 表示整套评分(用户提到"整套/一套/全部/5 件/总评",或图中明显有多件)。默认 single。
        """
        if not self._has_scoring_intent(event):
            logger.debug("score_wuwa_echo 被 LLM 调用但消息无评分意图关键词,已拒绝")
            return

        image_url, image_source = self._find_image(event)

        if not image_url:
            # 没图: 登记待办,等用户下一张图自动续上
            key = self._session_key(event)
            self._pending_requests[key] = (time.time(), character.strip(), mode)
            yield event.plain_result(
                "好的,请把声骸截图发过来,我会自动续上评分。"
                "(5 分钟内有效)"
            )
            return

        if not character.strip():
            yield event.plain_result(
                "请告诉我这件声骸是给哪个角色用的(角色名或昵称都行,例如「今汐」「维妈」「风主」)。"
            )
            return

        canonical, tip = self._resolve_character(character)
        if image_source == "cache":
            tip = "(使用了你刚才发的图)\n" + tip

        async for chunk in self._do_score(event, image_url, canonical, mode, tip):
            yield chunk

    @filter.llm_tool(name="list_wuwa_characters")
    async def tool_list_characters(self, event: AstrMessageEvent):
        """列出本插件支持的所有鸣潮角色权重模板。当用户询问"有哪些角色"、"支持谁"、"角色列表"时调用。
        """
        chars = self.scorer._raw.get("characters", {})
        canonical = [n for n in chars if not n.startswith("generic")]
        generic = [n for n in chars if n.startswith("generic")]
        lines = [f"已配置 {len(canonical)} 个角色 + {len(generic)} 个兜底模板:"]
        if canonical:
            lines.append("\n角色:")
            for i in range(0, len(canonical), 5):
                lines.append("  " + "  ".join(canonical[i:i + 5]))
        if generic:
            lines.append("\n兜底模板:")
            for name in generic:
                lines.append(f"  · {name}")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="show_wuwa_aliases")
    async def tool_show_aliases(self, event: AstrMessageEvent, character: str):
        """查看某个鸣潮角色的所有可用昵称/别名。当用户问"X 有什么别名"、"X 的昵称"时调用。

        Args:
            character(string): 角色名或任意已知昵称。
        """
        result = self.scorer.resolve(character)
        if result is None:
            yield event.plain_result(f"未识别到角色「{character}」。")
            return
        aliases = self.scorer.resolver.aliases_of(result.canonical)
        if not aliases:
            yield event.plain_result(f"{result.canonical}: 暂无别名记录。")
            return
        yield event.plain_result(
            f"{result.canonical} 的可用别名 ({len(aliases)} 个):\n"
            + "、".join(aliases)
        )

    @filter.llm_tool(name="reload_wuwa_weights")
    async def tool_reload(self, event: AstrMessageEvent):
        """重新加载鸣潮声骸权重配置(weights.json / aliases.json)。当用户提到"重载权重"、"更新配置"、"刷新角色表"时调用。
        """
        try:
            self.scorer.load()
            yield event.plain_result("权重表与别名表已重载。")
        except Exception as e:
            yield event.plain_result(f"重载失败: {e}")

    # ============== Passive listener: capture images & resume pending ==============

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """监听所有消息: 见图就缓存; 若有待办评分请求,自动续上评分。"""
        image_url = self._extract_image_url(event)
        if not image_url:
            return

        key = self._session_key(event)
        now = time.time()
        self._image_cache[key] = (now, image_url)
        self._prune_caches(now)

        pending = self._pending_requests.pop(key, None)
        if not pending:
            return

        ts, character, mode = pending
        if now - ts > self.CROSS_MESSAGE_TTL:
            return

        # 续评分
        if not character:
            await event.send(event.plain_result(
                "收到声骸图。请补充角色名(如「今汐」「维妈」),我立刻打分。"
            ))
            # 重新登记待办,这次有了 image,等用户补角色
            self._pending_requests[key] = (now, "", mode)
            return

        canonical, tip = self._resolve_character(character)
        tip = "(自动续上之前的评分请求)\n" + tip
        try:
            async for chunk in self._do_score(event, image_url, canonical, mode, tip):
                await event.send(chunk)
        except Exception as e:
            logger.exception("续办评分失败")
            await event.send(event.plain_result(f"评分失败: {e}"))

    # ===================== Helpers =====================

    def _resolve_character(self, raw: str) -> tuple[str, str]:
        query = (raw or "").strip()
        if not query:
            return self.default_character, f"已选角色: {self.default_character} (默认)"

        result = self.scorer.resolve(query)
        if result is None:
            return self.default_character, (
                f"未识别到角色「{query}」,使用默认模板「{self.default_character}」。"
            )

        canonical = result.canonical
        kind = result.kind
        if kind == "exact":
            tip = f"已选角色: {canonical}"
        elif kind == "alias":
            tip = f"已选角色: {canonical} (别名: {query})"
        elif kind == "substring":
            tip = f"已选角色: {canonical} (模糊匹配: {query})"
        else:
            tip = f"已选角色: {canonical} (猜测自: {query})"
        return canonical, tip

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
        """返回 (url, source); source 取值 'current' | 'cache' | None。"""
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
        for d in (self._image_cache, self._pending_requests):
            stale = [k for k, v in d.items() if now - v[0] > ttl]
            for k in stale:
                d.pop(k, None)

    async def _do_score(
        self,
        event: AstrMessageEvent,
        image_url: str,
        canonical: str,
        mode: str,
        tip: str,
    ):
        try:
            if mode == "set":
                result = await self._score_set_from_image(image_url, canonical)
                yield event.plain_result(
                    tip + "\n\n" + self._format_set_result(result, canonical)
                )
            else:
                result = await self._score_single_from_image(image_url, canonical)
                yield event.plain_result(
                    tip + "\n\n" + self._format_single_result(result, canonical)
                )
        except Exception as e:
            logger.exception("声骸评分失败")
            yield event.plain_result(f"评分失败: {e}")

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

    def _get_vision_provider(self):
        """取用于识图的 provider。优先用配置的 vision_provider_id,回落到当前 provider。"""
        if self.vision_provider_id:
            try:
                p = self.context.get_provider_by_id(self.vision_provider_id)
                if p:
                    return p
                logger.warning(
                    f"vision_provider_id='{self.vision_provider_id}' 查无此 provider,"
                    f"回落到当前 provider"
                )
            except Exception as e:
                logger.warning(
                    f"加载 vision provider '{self.vision_provider_id}' 失败: {e},"
                    f"回落到当前 provider"
                )
        p = self.context.get_using_provider()
        if not p:
            raise RuntimeError(
                "没有可用的 LLM provider,请在 AstrBot WebUI 配置一个多模态模型,"
                "或在插件配置 vision_provider_id 指向已配置的视觉 provider。"
            )
        return p

    async def _score_single_from_image(
        self, image_url: str, character: str
    ) -> ScoreResult:
        provider = self._get_vision_provider()
        raw = await vision_recognize(provider, image_url, SINGLE_ECHO_PROMPT_ZH)
        echo = parse_single(raw)
        return self.scorer.score_echo(echo, character)

    async def _score_set_from_image(
        self, image_url: str, character: str
    ) -> SetScoreResult:
        provider = self._get_vision_provider()
        raw = await vision_recognize(provider, image_url, SET_ECHO_PROMPT_ZH)
        echo_set = parse_set(raw)
        return self.scorer.score_set(echo_set, character)

    def _format_single_result(self, r: ScoreResult, character: str) -> str:
        lines = [
            f"【声骸评分 · {character}】",
            f"总分: {r.score} / 50    评级: {r.rank}",
            f"  词条种类: {r.quality_score} / 25",
            f"  词条数值: {r.value_score} / 25",
        ]
        if self.show_breakdown and r.breakdown:
            lines.append("\n副词条明细:")
            for b in r.breakdown:
                mark = "★" * min(int(b.weight), 3) if b.weight > 0 else "·"
                lines.append(
                    f"  {mark} {b.name} {b.value}  (权重 {b.weight}, {b.note})"
                )
        if r.comment:
            lines.append(f"\n点评: {r.comment}")
        return "\n".join(lines)

    def _format_set_result(self, r: SetScoreResult, character: str) -> str:
        lines = [
            f"【整套声骸 · {character}】",
            f"总分: {r.total_score} / 250    均分: {r.average_score} / 50    均评: {r.rank}",
            "",
        ]
        for idx, item in enumerate(r.items, 1):
            tag = "  (短板)" if idx - 1 == r.weakest_index else ""
            lines.append(f"#{idx}  {item.score} 分  {item.rank}{tag}")
        if r.comment:
            lines.append(f"\n{r.comment}")
        return "\n".join(lines)
