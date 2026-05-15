from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from .models import (
    Echo,
    EchoSet,
    ScoreResult,
    SetScoreResult,
    SubStatBreakdown,
)
from .resolver import CharacterResolver, ResolveResult


class EchoScorer:
    """声骸评分器。双指标制:词条种类分(0-25)+ 词条数值分(0-25),合计满分 50。

    评级阈值与权重表从 weights.json 加载,可热重载。
    """

    def __init__(self, weights_path: Path, aliases_path: Optional[Path] = None):
        self.weights_path = weights_path
        self.aliases_path = aliases_path or (weights_path.parent / "aliases.json")
        self._raw: dict = {}
        self.resolver = CharacterResolver(self.aliases_path)
        self.load()

    def load(self) -> None:
        with open(self.weights_path, "r", encoding="utf-8") as f:
            self._raw = json.load(f)
        self.resolver.load()

    def resolve(self, query: str) -> Optional[ResolveResult]:
        return self.resolver.resolve(query)

    @property
    def max_values(self) -> Dict[str, float]:
        return self._raw["max_values"]

    @property
    def rank_thresholds(self) -> Dict[str, float]:
        return self._raw["rank_thresholds"]

    def get_weights(self, character: str) -> Dict[str, int]:
        chars = self._raw["characters"]
        if character not in chars:
            result = self.resolver.resolve(character)
            if result and result.canonical in chars:
                character = result.canonical
            else:
                character = "generic_crit"
        entry = chars[character]
        if "_alias_of" in entry:
            entry = chars[entry["_alias_of"]]
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def rank_for(self, score: float) -> str:
        for rank, threshold in self.rank_thresholds.items():
            if score >= threshold:
                return rank
        return "N"

    def score_echo(self, echo: Echo, character: str) -> ScoreResult:
        weights = self.get_weights(character)
        max_values = self.max_values

        breakdown: list[SubStatBreakdown] = []
        total_weight = 0
        weighted_value_sum = 0.0
        weighted_value_max = 0.0

        for sub in echo.sub_stats:
            w = weights.get(sub.name, 0)
            mv = max_values.get(sub.name, 0.0)
            if mv <= 0:
                breakdown.append(SubStatBreakdown(
                    name=sub.name, value=sub.value, weight=w, score=0.0,
                    note="未知词条" if w == 0 else "缺少 max_value"
                ))
                continue

            ratio = min(sub.value / mv, 1.0)
            item_score = ratio * w
            breakdown.append(SubStatBreakdown(
                name=sub.name, value=sub.value, weight=w,
                score=round(item_score, 3),
                note=f"{round(ratio * 100, 1)}% 强度"
            ))

            total_weight += w
            weighted_value_sum += item_score
            weighted_value_max += w

        quality_score = self._quality_score(echo, weights)

        if weighted_value_max > 0:
            value_score = (weighted_value_sum / weighted_value_max) * 25
        else:
            value_score = 0.0

        total = round(quality_score + value_score, 2)
        rank = self.rank_for(total)

        return ScoreResult(
            score=total,
            rank=rank,
            quality_score=round(quality_score, 2),
            value_score=round(value_score, 2),
            breakdown=breakdown,
            comment=self._comment_for(total, rank, breakdown),
        )

    def _quality_score(self, echo: Echo, weights: Dict[str, int]) -> float:
        """词条种类得分:依据每个副词条的权重等级(3/2/1/0)加权,
        归一化到 0-25 分。理想情况是 5 条全是权重 3 的核心词条。"""
        if not echo.sub_stats:
            return 0.0
        total = sum(weights.get(s.name, 0) for s in echo.sub_stats)
        max_per_slot = 3
        full_potential = max_per_slot * 5
        return (total / full_potential) * 25

    def _comment_for(self, score: float, rank: str, breakdown: list[SubStatBreakdown]) -> str:
        useful = [b for b in breakdown if b.weight >= 2]
        useful_count = len(useful)

        if rank == "ACE":
            return f"毕业级别,{useful_count} 条核心词条,直接锁柜。"
        if rank == "SSS":
            return f"高质量,{useful_count} 条有效词条,推荐长期使用。"
        if rank == "SS":
            return "可用过渡,等更好的再换。"
        if rank == "S":
            return "勉强能用,词条偏弱。"
        if rank == "A":
            return "练度需求时凑合用,有更好的就替换。"
        return "建议直接分解。"

    def score_set(self, echo_set: EchoSet, character: str) -> SetScoreResult:
        items = [self.score_echo(e, character) for e in echo_set.echoes]
        if not items:
            return SetScoreResult(total_score=0, average_score=0, rank="N", items=[])

        total = sum(i.score for i in items)
        avg = total / len(items)
        rank = self.rank_for(avg)
        weakest_idx = min(range(len(items)), key=lambda i: items[i].score)

        comment = self._set_comment(items, weakest_idx)
        return SetScoreResult(
            total_score=round(total, 2),
            average_score=round(avg, 2),
            rank=rank,
            items=items,
            weakest_index=weakest_idx,
            comment=comment,
        )

    def _set_comment(self, items: list[ScoreResult], weakest_idx: int) -> str:
        if len(items) < 5:
            return f"只识别到 {len(items)} 件声骸,完整一套需要 5 件。"
        weak = items[weakest_idx]
        if weak.score < 18:
            return f"第 {weakest_idx + 1} 件是短板({weak.score} 分,{weak.rank}),优先替换它。"
        return "整体均衡,继续养。"
