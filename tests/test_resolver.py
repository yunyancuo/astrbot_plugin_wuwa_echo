"""角色名与别名解析器（CharacterResolver）单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.resolver import CharacterResolver, normalize_term

REPO_ROOT = Path(__file__).resolve().parent.parent
ALIASES_PATH = REPO_ROOT / "data" / "aliases.json"


def test_normalize_term():
    """验证大小写、全角半角与多种中点分隔符归一化。"""
    assert normalize_term("陆·赫斯") == "陆赫斯"
    assert normalize_term("陆・赫斯") == "陆赫斯"
    assert normalize_term("陆.赫斯") == "陆赫斯"
    assert normalize_term("陆 赫斯") == "陆赫斯"
    assert normalize_term("  Lucyna  ") == "lucyna"
    assert normalize_term("") == ""


def test_resolve_four_tier_fallback():
    """验证 exact -> alias -> substring -> fuzzy 四级回落策略。"""
    resolver = CharacterResolver(ALIASES_PATH)

    # 1. exact 精确命中规范名
    r_exact = resolver.resolve("今汐")
    assert r_exact is not None
    assert r_exact.canonical == "今汐"
    assert r_exact.kind == "exact"

    # 2. alias 大小写与中点归一化命中
    for query, expected in [
        ("JK", "千咲"),
        ("jk", "千咲"),
        ("VERINA", "维里奈"),
        ("Lucyna", "露西"),
        ("lucyna", "露西"),
        ("风主", "漂泊者·气动"),
        ("漂泊者 气动", "漂泊者·气动"),
        ("漂泊者-气动", "漂泊者·气动"),
        ("陆.赫斯", "陆·赫斯"),
        ("秧秧 玄翎", "秧秧·玄翎"),
    ]:
        res = resolver.resolve(query)
        assert res is not None, f"未能解析 {query!r}"
        assert res.canonical == expected
        assert res.kind == "alias"

    # 3. substring 双向子串匹配（被包含 & 包含词条）
    r_sub_in = resolver.resolve("卡提希")
    assert r_sub_in is not None
    assert r_sub_in.canonical == "卡提希娅"
    assert r_sub_in.kind == "substring"

    r_sub_contains = resolver.resolve("今汐声骸")
    assert r_sub_contains is not None
    assert r_sub_contains.canonical == "今汐"
    assert r_sub_contains.kind == "substring"

    # 4. fuzzy 相似度容错
    r_fuzzy = resolver.resolve("verinna")
    assert r_fuzzy is not None
    assert r_fuzzy.canonical == "维里奈"
    assert r_fuzzy.kind == "fuzzy"

    # 空串与无关词返回 None
    assert resolver.resolve("") is None
    assert resolver.resolve("   ") is None
    assert resolver.resolve("完全不存在的角色名称xyz") is None


def test_extract_from_text_longest_match_and_word_boundary():
    """验证自由文本提取最长优先防遮蔽与 ASCII 词边界防误触。"""
    resolver = CharacterResolver(ALIASES_PATH)

    # 回归：长规范名"秧秧·玄翎"绝不能被短规范名"秧秧"遮蔽
    assert resolver.extract_from_text("秧秧·玄翎") == "秧秧·玄翎"
    assert resolver.extract_from_text("秧秧 玄翎") == "秧秧·玄翎"
    assert resolver.extract_from_text("秧秧.玄翎") == "秧秧·玄翎"
    assert resolver.extract_from_text("秧秧玄翎") == "秧秧·玄翎"
    assert resolver.extract_from_text("普通秧秧") == "秧秧"

    # 回归：长规范名"鉴心"绝不能被单字规范名"心"遮蔽
    assert resolver.extract_from_text("鉴心") == "鉴心"

    # 回归：ASCII 2 字母别名不得在英文单词内部误触发
    for english_noise in [
        "class",
        "boss",
        "damage",
        "black",
        "feedback",
        "example",
        "daily",
        "online",
        "this is a test",
    ]:
        assert resolver.extract_from_text(english_noise) == "", f"{english_noise!r} 不应误命中角色"

    # 正常 ASCII 缩写（独立或中英紧邻）必须命中
    assert resolver.extract_from_text("jx") == "今汐"
    assert resolver.extract_from_text("帮我看下jx的") == "今汐"
    assert resolver.extract_from_text("verina 怎么样") == "维里奈"
    assert resolver.extract_from_text("kkl") == "卡卡罗"


def test_extract_from_text_single_char_canonical_guard():
    """验证单字中文角色名在正常触发与普通汉语词汇中的防误触平衡。"""
    resolver = CharacterResolver(ALIASES_PATH)

    # 正常提及单字角色名应命中
    assert resolver.extract_from_text("椿") == "椿"
    assert resolver.extract_from_text("给椿的") == "椿"
    assert resolver.extract_from_text("帮我看看 椿 的") == "椿"
    assert resolver.extract_from_text("心") == "心"
    assert resolver.extract_from_text("给心") == "心"

    # 嵌在普通汉语词内部不应误命中单字角色"心"
    assert resolver.extract_from_text("这件真让人伤心") == ""
    assert resolver.extract_from_text("好开心呀") == ""
    assert resolver.extract_from_text("核心词条") == ""


def test_resolver_missing_and_corrupted_reload(tmp_path: Path):
    """验证文件缺失时安全降级为空表，热重载遇到坏 JSON 时保留旧内存索引。"""
    missing = tmp_path / "missing_aliases.json"
    resolver = CharacterResolver(missing)
    assert resolver.canonical_names() == []
    assert resolver.resolve("今汐") is None

    # 写入合法别名表并加载
    good = tmp_path / "aliases.json"
    good.write_text(
        json.dumps({"今汐": ["今汐", " 汐汐 ", "JX", "jx", ""]}, ensure_ascii=False),
        encoding="utf-8-sig",
    )
    resolver = CharacterResolver(good)
    assert resolver.aliases_of("今汐") == ["今汐", "汐汐", "JX"]
    assert resolver.resolve("jx").canonical == "今汐"

    # 覆盖损坏 JSON 后热重载抛错，但旧内存索引不受破坏
    good.write_text("{broken json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        resolver.load()
    assert resolver.resolve("今汐").canonical == "今汐"

    # 非 dict 根节点抛 ValueError，旧内存索引仍完好
    good.write_text('["not", "a", "dict"]', encoding="utf-8")
    with pytest.raises(ValueError):
        resolver.load()
    assert resolver.resolve("汐汐").canonical == "今汐"
