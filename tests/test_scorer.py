"""声骸评分引擎（EchoScorer）数学逻辑、精度与边界测试。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from core.models import Echo, EchoSet, ScoreResult, SubStat
from core.scorer import EchoScorer

REPO_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_PATH = REPO_ROOT / "data" / "weights.json"
ALIASES_PATH = REPO_ROOT / "data" / "aliases.json"


def _make_echo(subs: list[tuple[str, float]], *, cost: int = 4) -> Echo:
    return Echo(
        cost=cost,
        main_stat="暴击伤害" if cost == 4 else "攻击%",
        main_stat_value=44.0 if cost == 4 else 30.0,
        level=25,
        sub_stats=[SubStat(name=n, value=v) for n, v in subs],
    )


def test_perfect_and_zero_score(tmp_path: Path):
    """验证满配全核心词条得分 50 (ACE) 与全无用词条得分 0 (N) 及除零防护。"""
    weights_file = tmp_path / "weights.json"
    weights_file.write_text(
        json.dumps(
            {
                "max_values": {
                    "暴击": 10.5,
                    "暴击伤害": 21.0,
                    "攻击%": 11.6,
                    "共鸣技能伤害加成": 11.6,
                    "共鸣解放伤害加成": 11.6,
                    "防御": 70.0,
                },
                "characters": {
                    "generic_crit": {
                        "暴击": 3,
                        "暴击伤害": 3,
                        "攻击%": 3,
                        "共鸣技能伤害加成": 3,
                        "共鸣解放伤害加成": 3,
                        "防御": 0,
                    }
                },
                "rank_thresholds": {"ACE": 45, "SSS": 35, "SS": 25, "S": 18, "A": 10, "N": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    scorer = EchoScorer(weights_file, tmp_path / "aliases.json")

    perfect = _make_echo(
        [
            ("暴击", 10.5),
            ("暴击伤害", 21.0),
            ("攻击%", 11.6),
            ("共鸣技能伤害加成", 11.6),
            ("共鸣解放伤害加成", 11.6),
        ]
    )
    r_perf = scorer.score_echo(perfect, "generic_crit")
    assert r_perf.quality_score == 25.0
    assert r_perf.value_score == 25.0
    assert r_perf.score == 50.0
    assert r_perf.rank == "ACE"
    assert "毕业" in r_perf.comment

    useless = _make_echo([("防御", 70.0)])
    r_zero = scorer.score_echo(useless, "generic_crit")
    assert r_zero.quality_score == 0.0
    assert r_zero.value_score == 0.0
    assert r_zero.score == 0.0
    assert r_zero.rank == "N"

    empty = Echo(cost=4, sub_stats=[])
    r_empty = scorer.score_echo(empty, "generic_crit")
    assert r_empty.score == 0.0
    assert r_empty.rank == "N"


def test_rounding_consistency_single_and_set(tmp_path: Path):
    """验证单件分项之和恒等于总分，以及整套展示均分与评级完全一致。"""
    scorer = EchoScorer(WEIGHTS_PATH, ALIASES_PATH)

    # 构造易出现 16.666... + 18.336... 四舍五入偏差的组合（今汐权重：暴击3/暴伤3/攻击%2/技能2 = 10 -> quality=16.6667）
    echo = _make_echo(
        [
            ("暴击", 7.5),
            ("暴击伤害", 15.0),
            ("攻击%", 8.6),
            ("共鸣技能伤害加成", 8.6),
            ("防御", 50.0),
        ]
    )
    res = scorer.score_echo(echo, "今汐")
    assert round(res.quality_score + res.value_score, 2) == res.score

    # 回归测试：整套均分 34.996 -> 展示均分 35.0，评级必须为 SSS 而非 SS
    weights_file = tmp_path / "weights.json"
    weights_file.write_text(WEIGHTS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    custom_scorer = EchoScorer(weights_file, ALIASES_PATH)

    # 手工构造一组 score，确认 score_set 按展示均分定级
    echo_high = _make_echo(
        [
            ("暴击", 10.5),
            ("暴击伤害", 21.0),
            ("攻击%", 11.6),
            ("共鸣技能伤害加成", 11.6),
            ("攻击", 60.0),
        ]
    )
    echo_mid = _make_echo(
        [
            ("暴击", 6.3),
            ("暴击伤害", 12.6),
            ("攻击%", 6.4),
            ("防御", 40.0),
            ("生命", 320.0),
        ]
    )
    set_res = custom_scorer.score_set(EchoSet(echoes=[echo_high, echo_mid]), "今汐")
    assert set_res.average_score == round(set_res.total_score / 2, 2)
    assert set_res.rank == custom_scorer.rank_for(set_res.average_score)
    assert set_res.weakest_index == 1
    assert "完整一套" in set_res.comment


def test_negative_overflow_and_unknown_substats():
    """验证负数值、超满值、超过 5 条副词条、未知词条的钳制与边界防护。"""
    scorer = EchoScorer(WEIGHTS_PATH, ALIASES_PATH)

    # 负数值与超满值
    echo_edge = _make_echo(
        [
            ("暴击", -5.0),
            ("暴击伤害", 99.0),
            ("不存在词条", 10.0),
            ("攻击%", 11.6),
            ("共鸣技能伤害加成", 11.6),
        ]
    )
    res = scorer.score_echo(echo_edge, "今汐")
    assert 0.0 <= res.quality_score <= 25.0
    assert 0.0 <= res.value_score <= 25.0
    assert 0.0 <= res.score <= 50.0
    assert res.breakdown[0].score == 0.0
    assert res.breakdown[1].note == "100.0% 强度"
    assert res.breakdown[2].note == "未知词条"

    # 即使绕过 OCR 直接传入 >5 条核心词条，种类分也不超过 25
    over_echo = _make_echo([("暴击", 10.5)] * 8)
    r_over = scorer.score_echo(over_echo, "今汐")
    assert r_over.quality_score == 25.0
    assert r_over.score == 50.0

    # rank_for 对 NaN 安全降级为 N
    assert scorer.rank_for(float("nan")) == "N"
    assert scorer.rank_for(-1.0) == "N"
    assert scorer.rank_for(45.0) == "ACE"
    assert scorer.rank_for(35.0) == "SSS"
    assert scorer.rank_for(25.0) == "SS"
    assert scorer.rank_for(18.0) == "S"
    assert scorer.rank_for(10.0) == "A"


def test_alias_of_chain_cycle_and_bom_reload(tmp_path: Path):
    """验证 _alias_of 多跳、循环/悬空引用防护与 UTF-8 BOM 热重载。"""
    wf = tmp_path / "weights.json"
    af = tmp_path / "aliases.json"
    af.write_text("{}", encoding="utf-8")

    payload = {
        "max_values": {"暴击": 10.5},
        "characters": {
            "generic_crit": {"暴击": 3},
            "A": {"_alias_of": "B"},
            "B": {"_alias_of": "generic_crit"},
            "Cycle1": {"_alias_of": "Cycle2"},
            "Cycle2": {"_alias_of": "Cycle1"},
            "Dangling": {"_alias_of": "NoSuchChar"},
        },
        "rank_thresholds": {"ACE": 45, "SSS": 35, "SS": 25, "S": 18, "A": 10, "N": 0},
    }
    # 以带 UTF-8 BOM 格式写入（模拟 Windows 记事本保存）
    wf.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")

    scorer = EchoScorer(wf, af)
    assert scorer.get_weights("A") == {"暴击": 3.0}
    assert scorer.get_weights("Cycle1") == {"暴击": 3.0}
    assert scorer.get_weights("Dangling") == {"暴击": 3.0}

    # 坏文件热重载抛错，但内存里旧权重保持完好
    wf.write_text('{"bad": true}', encoding="utf-8")
    with pytest.raises(ValueError):
        scorer.load()
    assert scorer.get_weights("A") == {"暴击": 3.0}


def test_score_set_five_items_weakest_and_balanced():
    """验证 5 件完整套装短板识别（<18 分）与均衡评价（>=18 分）。"""
    scorer = EchoScorer(WEIGHTS_PATH, ALIASES_PATH)
    good = _make_echo(
        [
            ("暴击", 9.3),
            ("暴击伤害", 18.6),
            ("攻击%", 10.1),
            ("共鸣技能伤害加成", 9.4),
            ("攻击", 40.0),
        ]
    )
    weak = _make_echo(
        [
            ("防御", 40.0),
            ("生命", 320.0),
            ("防御%", 8.1),
            ("生命%", 6.4),
            ("攻击", 30.0),
        ]
    )

    res_weak = scorer.score_set(EchoSet(echoes=[good, good, weak, good, good]), "今汐")
    assert res_weak.weakest_index == 2
    assert "第 3 件是短板" in res_weak.comment

    res_balanced = scorer.score_set(EchoSet(echoes=[good] * 5), "今汐")
    assert res_balanced.comment == "整体均衡,继续养。"

    res_empty = scorer.score_set(EchoSet(echoes=[]), "今汐")
    assert res_empty.total_score == 0.0
    assert res_empty.rank == "N"
    assert res_empty.weakest_index == -1


def test_substat_model_rejects_non_finite():
    """验证 SubStat 模型层拒绝 NaN 与 Infinity。"""
    with pytest.raises(ValueError):
        SubStat(name="暴击", value=float("nan"))
    with pytest.raises(ValueError):
        SubStat(name="暴击", value=math.inf)


def test_missing_generic_crit_and_set_rank_boundary(tmp_path: Path):
    """验证缺少 generic_crit 不崩溃，且整套均分 34.996 -> 35.0 准确评为 SSS。"""
    wf = tmp_path / "weights.json"
    af = tmp_path / "aliases.json"
    af.write_text("{}", encoding="utf-8")
    wf.write_text(
        json.dumps(
            {
                "max_values": {"暴击": 10.5},
                "characters": {"仅有一人": {"暴击": 3}},
                "rank_thresholds": {"ACE": 45, "SSS": 35, "SS": 25, "S": 18, "A": 10, "N": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    scorer = EchoScorer(wf, af, default_character="不存在默认")
    assert scorer.get_weights("未知角色") == {}

    # 构造 5 件声骸使其精确均分为 34.996，四舍五入展示 35.0，评级必须为 SSS
    scores = iter([35.0, 35.0, 35.0, 35.0, 34.98])

    def _fake_score_echo(_echo: Echo, _character: str) -> ScoreResult:
        val = next(scores)
        return ScoreResult(
            score=val,
            rank=scorer.rank_for(val),
            quality_score=15.0,
            value_score=round(val - 15.0, 2),
        )

    scorer.score_echo = _fake_score_echo  # type: ignore[method-assign]
    res = scorer.score_set(EchoSet(echoes=[_make_echo([]) for _ in range(5)]), "仅有一人")
    assert res.total_score == 174.98
    assert res.average_score == 35.0
    assert res.rank == "SSS"
