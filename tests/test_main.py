"""插件主入口（main.py）路由门控、LLM 阻断兼容性、跨消息续办与防抖重试单元测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeImage:
    """模拟 AstrBot Image 消息组件。"""

    def __init__(self, *, url: str | None = None, file: str | None = None):
        self.url = url
        self.file = file


class _FakePlain:
    """模拟 AstrBot 文本消息组件。"""

    def __init__(self, text: str):
        self.text = text


class _FakeLogger:
    """静默收集日志以便断言。"""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def info(self, msg: str) -> None:
        self.records.append(("info", msg))

    def warning(self, msg: str) -> None:
        self.records.append(("warning", msg))

    def exception(self, msg: str) -> None:
        self.records.append(("exception", msg))


class _FakeEvent:
    """模拟 AstrMessageEvent，完整记录 stop_event / should_call_llm / 标志与发送消息。"""

    def __init__(
        self,
        *,
        text: str = "",
        image_url: str | None = None,
        is_private: bool = True,
        is_at: bool = True,
        session_id: str = "sess:1",
    ):
        self.message_str = text
        components = []
        if image_url is not None:
            components.append(_FakeImage(url=image_url))
        if text:
            components.append(_FakePlain(text))
        self.message_obj = SimpleNamespace(message=components)
        self._is_private = is_private
        self.is_at_or_wake_command = is_at
        self.unified_msg_origin = session_id

        self.stopped = False
        self.should_call_llm_args: list[bool] = []
        self._has_send_oper = False
        self.call_llm = False
        self.sent: list[str] = []
        self.timeline: list[str] = []

    def is_private_chat(self) -> bool:
        return self._is_private

    def stop_event(self) -> None:
        self.stopped = True
        self.timeline.append("stop_event")

    def should_call_llm(self, flag: bool) -> None:
        self.should_call_llm_args.append(flag)
        self.timeline.append(f"should_call_llm({flag})")

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result: str) -> None:
        self.sent.append(result)
        self.timeline.append("send")


class _FakeProvider:
    """模拟多模态视觉 provider。"""

    def __init__(self, payload: str | Exception, *, delay: float = 0.0):
        self.payload = payload
        self.delay = delay
        self.calls = 0

    async def text_chat(self, *, prompt: str, image_urls: list[str]):
        self.calls += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if isinstance(self.payload, Exception):
            raise self.payload
        return SimpleNamespace(completion_text=self.payload)


class _FakeContext:
    """模拟 AstrBot Context。"""

    def __init__(self, provider: _FakeProvider | None = None):
        self.provider = provider
        self.by_id: dict[str, _FakeProvider] = {}

    def get_provider_by_id(self, pid: str):
        return self.by_id.get(pid)

    def get_using_provider(self):
        return self.provider


def _load_plugin_module():
    """注入轻量级 AstrBot 桩模块并按包上下文加载 main.py（100% 离线）。"""
    pkg_name = "astrbot_plugin_wuwa_echo_pkg"
    if f"{pkg_name}.main" in sys.modules:
        return sys.modules[f"{pkg_name}.main"]

    # 构建 astrbot 桩模块树
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = _FakeLogger()

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = _FakeEvent

    class _FakeFilter:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def event_message_type(_kind):
            def _dec(fn):
                return fn

            return _dec

    event_mod.filter = _FakeFilter

    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Image = _FakeImage

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = _FakeContext

    class _FakeStar:
        def __init__(self, context):
            self.context = context

    star_mod.Star = _FakeStar
    star_mod.register = lambda *args, **kwargs: (lambda cls: cls)

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules["astrbot.api.star"] = star_mod

    # 挂载虚拟根包与子包 core，使 main.py 的相对导入 `from .core...` 原样工作
    root_pkg = types.ModuleType(pkg_name)
    root_pkg.__path__ = [str(REPO_ROOT)]
    sys.modules[pkg_name] = root_pkg

    spec = importlib.util.spec_from_file_location(f"{pkg_name}.main", REPO_ROOT / "main.py")
    assert spec is not None and spec.loader is not None
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.main"] = main_mod
    spec.loader.exec_module(main_mod)
    return main_mod


def _valid_single_ocr_json() -> str:
    return json.dumps(
        {
            "cost": 4,
            "set_name": "浮星祛暗",
            "main_stat": "暴击伤害",
            "main_stat_value": 44.0,
            "level": 25,
            "sub_stats": [
                {"name": "暴击", "value": 9.3},
                {"name": "暴击伤害", "value": 18.6},
                {"name": "攻击%", "value": 10.1},
                {"name": "共鸣技能伤害加成", "value": 9.4},
                {"name": "攻击", "value": 40.0},
            ],
        },
        ensure_ascii=False,
    )


def _valid_set_ocr_json(count: int = 3) -> str:
    item = json.loads(_valid_single_ocr_json())
    return json.dumps({"echoes": [item] * count}, ensure_ascii=False)


def _make_plugin(provider: _FakeProvider | None = None, config: dict | None = None):
    main_mod = _load_plugin_module()
    ctx = _FakeContext(provider or _FakeProvider(_valid_single_ocr_json()))
    cfg = {"auto_sync": False}
    if config:
        cfg.update(config)
    return main_mod, main_mod.WuwaEchoPlugin(ctx, cfg)


def test_group_at_gate_blocks_unaddressed_intent_penetration():
    """回归测试：群聊未 @ bot 时，即使包含强意图词也不得回复、不得写 pending、不得阻断 LLM。"""
    _, plugin = _make_plugin()
    ev = _FakeEvent(
        text="大家觉得这件今汐声骸评分怎么样",
        image_url=None,
        is_private=False,
        is_at=False,
        session_id="group:100",
    )
    asyncio.run(plugin.on_any_message(ev))

    assert ev.stopped is False
    assert ev.should_call_llm_args == []
    assert ev._has_send_oper is False
    assert ev.call_llm is False
    assert ev.sent == []
    assert "group:100" not in plugin._pending


def test_group_image_first_then_at_scores_and_preserves_llm_blocking_sequence():
    """验证群聊先发图（不拦不回）→ 再 @bot 评分，且严格保持 stop_event + _mark_no_llm + sleep(0) 在 OCR 之前。"""
    order: list[str] = []

    class _TracingProvider(_FakeProvider):
        async def text_chat(self, *, prompt: str, image_urls: list[str]):
            order.append("ocr_start")
            return await super().text_chat(prompt=prompt, image_urls=image_urls)

    provider = _TracingProvider(_valid_single_ocr_json())
    _, plugin = _make_plugin(provider)

    # 1. 群聊未 @ 纯发图：只缓存、不拦、不回复
    ev_img = _FakeEvent(
        text="",
        image_url="https://img.example/echo1.png",
        is_private=False,
        is_at=False,
        session_id="group:200",
    )
    asyncio.run(plugin.on_any_message(ev_img))
    assert ev_img.stopped is False
    assert ev_img.sent == []
    assert "group:200" in plugin._image_cache

    # 2. 随后 @bot 触发评分：必须先 stop_event + should_call_llm(True) + _has_send_oper + call_llm 再跑 OCR
    ev_score = _FakeEvent(
        text="eCho 评分 秧秧·玄翎",
        image_url=None,
        is_private=False,
        is_at=True,
        session_id="group:200",
    )
    orig_stop = ev_score.stop_event

    def _wrapped_stop():
        order.append("stop_event")
        orig_stop()

    ev_score.stop_event = _wrapped_stop

    asyncio.run(plugin.on_any_message(ev_score))

    assert ev_score.stopped is True
    assert ev_score.should_call_llm_args == [True]
    assert ev_score._has_send_oper is True
    assert ev_score.call_llm is True
    assert order == ["stop_event", "ocr_start"]
    assert len(ev_score.sent) == 1
    # 同时验证"秧秧·玄翎"没有被"秧秧"遮蔽
    assert "【秧秧·玄翎】" in ev_score.sent[0]


def test_weak_intent_and_english_word_false_positive_guards():
    """验证弱意图词日常闲聊放行，以及英文单词不会触发角色误匹配。"""
    _, plugin = _make_plugin()

    # 私聊日常弱意图词（无图无角色）完全放行
    ev_chat = _FakeEvent(text="今天天气怎么样", is_private=True, session_id="priv:1")
    asyncio.run(plugin.on_any_message(ev_chat))
    assert ev_chat.stopped is False
    assert ev_chat.sent == []

    # 包含英文词 class/boss + 评分，不应误判为角色"长离"/"穗穗"
    ev_en = _FakeEvent(text="class boss 评分", is_private=True, session_id="priv:2")
    asyncio.run(plugin.on_any_message(ev_en))
    assert ev_en.stopped is True
    assert len(ev_en.sent) == 1
    assert "告诉我角色名" in ev_en.sent[0]


def test_pending_step_by_step_completion_and_chat_passthrough():
    """验证跨消息分步补齐（空意图 -> 补角色 -> 无关闲聊放行 -> 补图完成评分）。"""
    _, plugin = _make_plugin()

    # Step 1: 发"评分"缺图缺角色
    ev1 = _FakeEvent(text="评分", is_private=False, is_at=True, session_id="group:300")
    asyncio.run(plugin.on_any_message(ev1))
    assert ev1.stopped is True
    assert "请发一张声骸截图" in ev1.sent[0]

    # Step 2: 补角色名（无需 @）-> 推进并提示发截图
    ev2 = _FakeEvent(text="今汐", is_private=False, is_at=False, session_id="group:300")
    asyncio.run(plugin.on_any_message(ev2))
    assert ev2.stopped is True
    assert "角色已记(今汐)" in ev2.sent[0]

    # Step 3: pending 期间无关闲聊 -> 完全放行不吞消息
    ev_chat = _FakeEvent(text="哈哈哈真不错", is_private=False, is_at=False, session_id="group:300")
    asyncio.run(plugin.on_any_message(ev_chat))
    assert ev_chat.stopped is False
    assert ev_chat.sent == []

    # Step 4: 补图（无需 @）-> 完成评分并清除 pending
    ev3 = _FakeEvent(
        text="",
        image_url="https://img.example/echo2.png",
        is_private=False,
        is_at=False,
        session_id="group:300",
    )
    asyncio.run(plugin.on_any_message(ev3))
    assert ev3.stopped is True
    assert "【今汐】" in ev3.sent[0]
    assert "group:300" not in plugin._pending


def test_repeat_guard_and_retry_unlocked_on_timeout_and_validation_error():
    """回归测试：成功评分占用 90s 防重窗口；超时或校验失败必须释放窗口允许立即重试。"""
    # 1. 超时后立即重试应允许执行
    slow_provider = _FakeProvider(_valid_single_ocr_json(), delay=0.2)
    _, plugin = _make_plugin(slow_provider)
    plugin.vision_timeout = 0.02

    ev_t1 = _FakeEvent(
        text="今汐评分", image_url="https://img.example/retry.png", session_id="priv:retry"
    )
    asyncio.run(plugin.on_any_message(ev_t1))
    assert "超时" in ev_t1.sent[0]
    assert ("https://img.example/retry.png", "今汐", "single") not in plugin._recent_requests

    # 恢复速度立刻重试同一张图，应成功评分
    slow_provider.delay = 0.0
    plugin.vision_timeout = 5.0
    ev_t2 = _FakeEvent(
        text="今汐评分", image_url="https://img.example/retry.png", session_id="priv:retry"
    )
    asyncio.run(plugin.on_any_message(ev_t2))
    assert "【今汐】" in ev_t2.sent[0]

    # 成功后 90s 内重复触发应静默忽略
    ev_dup = _FakeEvent(
        text="今汐评分", image_url="https://img.example/retry.png", session_id="priv:retry"
    )
    asyncio.run(plugin.on_any_message(ev_dup))
    assert ev_dup.sent == []
    assert slow_provider.calls == 2

    # 2. OCR 校验失败（ValueError）后同样释放窗口允许立刻重试
    bad_provider = _FakeProvider('{"cost": 4, "main_stat": "暴击", "sub_stats": []}')
    _, plugin2 = _make_plugin(bad_provider)
    ev_v1 = _FakeEvent(
        text="今汐评分", image_url="https://img.example/blur.png", session_id="priv:blur"
    )
    asyncio.run(plugin2.on_any_message(ev_v1))
    assert "不足 5 条" in ev_v1.sent[0]
    assert ("https://img.example/blur.png", "今汐", "single") not in plugin2._recent_requests


def test_set_mode_formatting_config_guards_cache_cap_and_lifecycle():
    """验证整套模式不重复缺件文案、异常配置下限保护、缓存上限淘汰与优雅生命周期关闭。"""
    # 整套识别 3 件，缺件提示不应重复出现两次
    _, plugin = _make_plugin(_FakeProvider(_valid_set_ocr_json(3)))
    ev_set = _FakeEvent(
        text="今汐整套打分", image_url="https://img.example/set.png", session_id="priv:set"
    )
    asyncio.run(plugin.on_any_message(ev_set))
    assert len(ev_set.sent) == 1
    assert "【今汐 · 整套】" in ev_set.sent[0]
    assert ev_set.sent[0].count("完整一套") == 1

    # 非法或负值配置不会崩溃，自动安全回落/钳制
    _, plugin_bad_cfg = _make_plugin(
        config={
            "vision_timeout_sec": -10,
            "sync_interval_hours": 0.01,
            "auto_sync": True,
        }
    )
    assert plugin_bad_cfg.vision_timeout == 45.0
    assert plugin_bad_cfg.sync_interval == 3600.0

    # 缓存容量上限淘汰
    plugin_bad_cfg.MAX_CACHE_ENTRIES = 3
    for i in range(5):
        plugin_bad_cfg._image_cache[f"k{i}"] = (1000.0 + i, f"https://img/{i}.png")
    plugin_bad_cfg._prune_caches(1005.0)
    assert len(plugin_bad_cfg._image_cache) == 3
    assert "k0" not in plugin_bad_cfg._image_cache and "k1" not in plugin_bad_cfg._image_cache

    # 生命周期 initialize & terminate 优雅启停且防重复创建任务
    async def _lifecycle():
        await plugin_bad_cfg.initialize()
        t1 = plugin_bad_cfg._sync_task
        assert t1 is not None
        await plugin_bad_cfg.initialize()
        assert plugin_bad_cfg._sync_task is t1
        await plugin_bad_cfg.terminate()
        assert plugin_bad_cfg._sync_task is None
        assert t1.cancelled()

    asyncio.run(_lifecycle())


def test_reuse_pending_character_provider_fallback_and_group_at_disabled():
    """验证沿用 pending 角色、provider 回落/缺失处理、私聊纯图静默与关闭群聊 @ 门控。"""
    # 1. 先"评分今汐"登记 pending，再发"[图] 评分"应沿用今汐并完成评分
    _, plugin = _make_plugin()
    ev1 = _FakeEvent(text="评分今汐", is_private=True, session_id="priv:reuse")
    asyncio.run(plugin.on_any_message(ev1))
    assert "角色已记(今汐)" in ev1.sent[0]

    ev2 = _FakeEvent(
        text="评分",
        image_url="https://img.example/reuse.png",
        is_private=True,
        session_id="priv:reuse",
    )
    asyncio.run(plugin.on_any_message(ev2))
    assert "【今汐】" in ev2.sent[0]

    # 2. 私聊纯发图无意图：缓存图片并拦 LLM，静默不回复
    ev_img_only = _FakeEvent(
        text="",
        image_url="https://img.example/silent.png",
        is_private=True,
        session_id="priv:silent",
    )
    asyncio.run(plugin.on_any_message(ev_img_only))
    assert ev_img_only.stopped is True
    assert ev_img_only.sent == []
    assert "priv:silent" in plugin._image_cache

    # 3. group_require_at=False 时群聊未 @ 也能触发
    _, plugin_no_at = _make_plugin(config={"group_require_at": False})
    ev_grp = _FakeEvent(
        text="今汐评分",
        image_url="https://img.example/grp.png",
        is_private=False,
        is_at=False,
        session_id="group:no_at",
    )
    asyncio.run(plugin_no_at.on_any_message(ev_grp))
    assert ev_grp.stopped is True
    assert "【今汐】" in ev_grp.sent[0]

    # 4. 指定不存在的 vision_provider_id 回落到默认 provider；全无 provider 返回错误且释放防重
    main_mod = _load_plugin_module()
    empty_ctx = _FakeContext(provider=None)
    plugin_no_prov = main_mod.WuwaEchoPlugin(
        empty_ctx, {"auto_sync": False, "vision_provider_id": "missing_id"}
    )
    ev_err = _FakeEvent(
        text="今汐评分",
        image_url="https://img.example/no_prov.png",
        is_private=True,
        session_id="priv:noprov",
    )
    asyncio.run(plugin_no_prov.on_any_message(ev_err))
    assert "评分出错了" in ev_err.sent[0]
    assert ("https://img.example/no_prov.png", "今汐", "single") not in plugin_no_prov._recent_requests
