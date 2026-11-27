"""数据文件与插件配置 Schema 完整性与交叉引用测试。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from core.ocr import SUBSTAT_MAX_VALUE
from core.scorer import EchoScorer
from core.sync import STANDARD_SUBSTATS, validate_aliases_data, validate_weights_data

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
WEIGHTS_PATH = DATA_DIR / "weights.json"
ALIASES_PATH = DATA_DIR / "aliases.json"
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"
METADATA_PATH = REPO_ROOT / "metadata.yaml"


def test_weights_json_schema_and_keys():
    """校验 weights.json 合法性、标准 13 词条一致性、权重范围与引用完整性。"""
    data = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8-sig"))
    assert validate_weights_data(data) == []

    # max_values 与 ocr.py / sync.py 13 标准副词条 100% 对齐
    assert set(data["max_values"].keys()) == set(STANDARD_SUBSTATS)
    assert data["max_values"] == SUBSTAT_MAX_VALUE

    chars = data["characters"]
    assert "generic_crit" in chars

    expected_stats = set(STANDARD_SUBSTATS)
    for cname, cfg in chars.items():
        if "_alias_of" in cfg:
            assert cfg["_alias_of"] in chars, f"{cname} 的 _alias_of 指向不存在的角色"
            continue
        stat_keys = {k for k in cfg if not k.startswith("_")}
        assert stat_keys == expected_stats, f"角色 {cname} 词条键不一致"
        for stat, weight in cfg.items():
            if stat.startswith("_"):
                continue
            assert isinstance(weight, (int, float)) and not isinstance(weight, bool)
            assert 0 <= weight <= 3, f"角色 {cname}.{stat} 权重越界: {weight}"


def test_aliases_json_schema_and_cleanliness():
    """校验 aliases.json 无空串、无首尾空白、同角色内无精确或大小写重复。"""
    aliases = json.loads(ALIASES_PATH.read_text(encoding="utf-8-sig"))
    assert validate_aliases_data(aliases) == []

    for canonical, terms in aliases.items():
        assert canonical == canonical.strip()
        assert canonical in terms, f"规范名 {canonical} 应在自身别名列表中"
        seen_lower: set[str] = set()
        for term in terms:
            assert term == term.strip(), f"{canonical} 含带首尾空白的别名: {term!r}"
            tl = term.lower()
            assert tl not in seen_lower, f"{canonical} 含重复别名: {term!r}"
            seen_lower.add(tl)


def test_aliases_and_weights_cross_reference_fallback():
    """校验所有别名表角色均能安全获取评分权重（未出 calc 的新角色回落默认模板）。"""
    aliases = json.loads(ALIASES_PATH.read_text(encoding="utf-8-sig"))
    weights = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8-sig"))
    scorer = EchoScorer(WEIGHTS_PATH, ALIASES_PATH)

    # 权重表中除 generic_crit 外的角色均在别名表中有登记
    weights_only = set(weights["characters"].keys()) - set(aliases.keys())
    assert weights_only == {"generic_crit"}

    # 每个别名表中的角色（含尚未出 calc 的新角色）调用 get_weights 都必须返回完整 13 词条
    for canonical in aliases:
        w = scorer.get_weights(canonical)
        assert set(w.keys()) == set(STANDARD_SUBSTATS)


def test_plugin_metadata_and_conf_schema():
    """校验 metadata.yaml 与 _conf_schema.json 的基础结构与默认值类型。"""
    meta = yaml.safe_load(METADATA_PATH.read_text(encoding="utf-8"))
    assert meta["name"] == "astrbot_plugin_wuwa_echo"
    assert meta["author"] == "yunyancuo"
    assert meta["version"]

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    expected_types = {
        "default_character": str,
        "vision_provider_id": str,
        "vision_timeout_sec": int,
        "group_require_at": bool,
        "auto_sync": bool,
        "sync_interval_hours": int,
        "show_substat_breakdown": bool,
    }
    assert set(schema.keys()) == set(expected_types.keys())
    for key, py_type in expected_types.items():
        assert isinstance(schema[key]["default"], py_type), f"配置项 {key} 默认值类型错误"
