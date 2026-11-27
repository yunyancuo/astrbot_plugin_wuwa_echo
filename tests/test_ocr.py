"""多模态 OCR 解析、词条容错纠偏、异常值防护与安全脱敏测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.ocr import (
    MAX_RAW_TEXT_LEN,
    _to_int,
    parse_set,
    parse_single,
    redact_sensitive,
    vision_recognize,
)


def _valid_echo_dict(**overrides) -> dict:
    base = {
        "cost": 4,
        "set_name": "凝夜白霜",
        "main_stat": "暴击伤害",
        "main_stat_value": 44.0,
        "level": 25,
        "sub_stats": [
            {"name": "暴击", "value": 9.3},
            {"name": "暴击伤害", "value": 18.6},
            {"name": "攻击%", "value": 10.1},
            {"name": "共鸣技能伤害加成", "value": 9.4},
            {"name": "共鸣效率", "value": 10.0},
        ],
    }
    base.update(overrides)
    return base


def test_parse_single_standard_markdown_and_trailing_braces():
    """验证纯 JSON、markdown 代码块及尾部含额外花括号文本时的精准提取（防贪婪正则 Bug）。"""
    payload = json.dumps(_valid_echo_dict(), ensure_ascii=False)

    # 1. 纯 JSON
    e1 = parse_single(payload)
    assert e1.cost == 4
    assert len(e1.sub_stats) == 5

    # 2. markdown 代码块 + 尾部说明里包含额外花括号
    wrapped = f"识别结果如下：\n```json\n{payload}\n```\n注：已忽略主词条 {{攻击 150}}。"
    e2 = parse_single(wrapped)
    assert e2.main_stat == "暴击伤害"
    assert len(e2.sub_stats) == 5

    # 3. 非代码块包裹但正文后含额外 JSON/花括号片段
    noisy = f"以下是识别数据：{payload}\n附加说明：{{参考值}}"
    e3 = parse_single(noisy)
    assert e3.level == 25
    assert len(e3.sub_stats) == 5


def test_parse_single_substat_aliases_percent_and_mainstat_filter():
    """验证 OCR 词条变体纠偏、带%字符串数值解析、百分号自动升级与 COST4 主词条过滤。"""
    raw = json.dumps(
        {
            "cost": "4",
            "set_name": "  浮星祛暗  ",
            "main_stat": "暴击",
            "main_stat_value": "22.0%",
            "level": "+25",
            "sub_stats": [
                {"name": "攻击", "value": 150},  # COST 4 固定主词条误识 -> 必须丢弃
                {"name": "+暴击率", "value": "+9.3%"},
                {"name": "爆伤", "value": "18.6％"},
                {"name": "攻击", "value": "10.1%"},  # value 带 % -> 自动纠偏为 攻击%
                {"name": "技能伤害加成", "value": "9.4%"},
                {"name": "大生命", "value": 8.6},
            ],
        },
        ensure_ascii=False,
    )
    echo = parse_single(raw)
    assert echo.set_name == "浮星祛暗"
    assert echo.main_stat_value == 22.0
    assert echo.level == 25
    assert [(s.name, s.value) for s in echo.sub_stats] == [
        ("暴击", 9.3),
        ("暴击伤害", 18.6),
        ("攻击%", 10.1),
        ("共鸣技能伤害加成", 9.4),
        ("生命%", 8.6),
    ]


def test_parse_single_nan_inf_bool_duplicate_and_overflow_guards():
    """验证 NaN/Inf/bool/负值过滤、同名去重、超过 5 条拦截与非法 cost/level 归一化。"""
    # _to_int 遇到 inf/nan/bool 绝不抛 OverflowError
    assert _to_int(float("inf"), 4) == 4
    assert _to_int(float("-inf"), 4) == 4
    assert _to_int(float("nan"), 4) == 4
    assert _to_int(True, 4) == 4
    assert _to_int("+25", 0) == 25

    # NaN / Inf / bool / 重复词条被清洗后剩余不足 5 条应报清晰错误
    bad_vals = json.dumps(
        _valid_echo_dict(
            sub_stats=[
                {"name": "暴击", "value": "NaN"},
                {"name": "暴击伤害", "value": "Infinity"},
                {"name": "攻击%", "value": True},
                {"name": "共鸣效率", "value": 9.2},
                {"name": "共鸣效率", "value": 8.4},  # 重复词条去重
                {"name": "攻击", "value": -10},
            ]
        ),
        ensure_ascii=False,
    )
    with pytest.raises(ValueError, match="不足 5 条"):
        parse_single(bad_vals)

    # 去重后超过 5 条副词条应拦截报错
    six_subs = _valid_echo_dict()
    six_subs["sub_stats"].append({"name": "攻击", "value": 40})
    with pytest.raises(ValueError, match="超过上限 5 条"):
        parse_single(json.dumps(six_subs, ensure_ascii=False))

    # COST 4 缺主词条应拦截
    no_main = _valid_echo_dict(main_stat="")
    with pytest.raises(ValueError, match="主词条"):
        parse_single(json.dumps(no_main, ensure_ascii=False))

    # 非法 cost(99) 与越界 level(999) / 非数字 main_stat_value 安全清洗
    odd_fields = _valid_echo_dict(cost=99, level=999, main_stat_value="N/A")
    echo = parse_single(json.dumps(odd_fields, ensure_ascii=False))
    assert echo.cost == 4
    assert echo.level == 25
    assert echo.main_stat_value is None


def test_parse_single_oversized_and_invalid_inputs():
    """验证超大文本防护、空输入与无 JSON 文本报错。"""
    with pytest.raises(ValueError, match="过长"):
        parse_single("{" + "a" * MAX_RAW_TEXT_LEN + "}")
    with pytest.raises(ValueError, match="为空"):
        parse_single("   ")
    with pytest.raises(ValueError, match="未找到有效 JSON"):
        parse_single("图片太模糊看不清")


def test_parse_set_tolerant_truncation_and_all_fail():
    """验证整套宽容解析（跳过坏件）、超 5 件截断与全失败提示。"""
    good = _valid_echo_dict()
    bad = _valid_echo_dict(sub_stats=[{"name": "暴击", "value": 8.1}])

    # 3 好 1 坏 -> 返回 3 件
    raw_partial = json.dumps({"echoes": [good, bad, good, good]}, ensure_ascii=False)
    echo_set = parse_set(raw_partial)
    assert len(echo_set.echoes) == 3

    # 模型幻觉输出 7 件合法项 -> 截断至最多 5 件
    raw_many = json.dumps({"echoes": [good] * 7}, ensure_ascii=False)
    echo_set_5 = parse_set(raw_many)
    assert len(echo_set_5.echoes) == 5

    # 全失败 -> 报含首件主要问题的中文错误，无重复“第 1 件”前缀
    raw_all_bad = json.dumps({"echoes": [bad, bad]}, ensure_ascii=False)
    with pytest.raises(ValueError) as exc_info:
        parse_set(raw_all_bad)
    msg = str(exc_info.value)
    assert "一件都没识别完整" in msg
    assert msg.count("第 1 件") == 1


def test_vision_recognize_and_redaction():
    """验证多模态调用成功、空返回、无效 URL、超时与敏感信息脱敏。"""

    class _FakeProvider:
        def __init__(self, response=None, delay: float = 0.0):
            self.response = response
            self.delay = delay
            self.calls = []

        async def text_chat(self, *, prompt: str, image_urls: list[str]):
            self.calls.append((prompt, image_urls))
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            return self.response

    # 正常返回 completion_text
    p_ok = _FakeProvider(SimpleNamespace(completion_text='{"ok": true}'))
    out = asyncio.run(vision_recognize(p_ok, "https://img.example/a.png", "prompt", timeout=5.0))
    assert out == '{"ok": true}'

    # 纯字符串响应兼容
    p_str = _FakeProvider('{"ok": 2}')
    assert asyncio.run(vision_recognize(p_str, "https://img.example/a.png", "prompt")) == '{"ok": 2}'

    # 空响应或 None 抛 ValueError
    p_empty = _FakeProvider(SimpleNamespace(completion_text="   "))
    with pytest.raises(ValueError, match="空内容"):
        asyncio.run(vision_recognize(p_empty, "https://img.example/a.png", "prompt"))

    p_none = _FakeProvider(None)
    with pytest.raises(ValueError, match="未返回识别结果"):
        asyncio.run(vision_recognize(p_none, "https://img.example/a.png", "prompt"))

    # 空 URL / 超长 URL 拦截
    with pytest.raises(ValueError, match="有效图片地址"):
        asyncio.run(vision_recognize(p_ok, "   ", "prompt"))
    with pytest.raises(ValueError, match="异常过长"):
        asyncio.run(vision_recognize(p_ok, "https://a.com/" + "x" * 5000, "prompt"))

    # 超时触发 asyncio.TimeoutError
    p_slow = _FakeProvider(SimpleNamespace(completion_text="{}"), delay=0.5)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(vision_recognize(p_slow, "https://img.example/a.png", "prompt", timeout=0.02))

    # 敏感信息脱敏
    leaked = "Authorization: Bearer abc.def.ghi sk-1234567890abcdef https://a.com/x?token=secret123&k=1"
    redacted = redact_sensitive(leaked)
    assert "abc.def.ghi" not in redacted
    assert "1234567890abcdef" not in redacted
    assert "secret123" not in redacted
    assert "k=1" in redacted
