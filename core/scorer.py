"""声骸双指标评分引擎。"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .models import (
    Echo,
    EchoSet,
    ScoreResult,
    SetScoreResult,
    SubStatBreakdown,
)
from .resolver import CharacterResolver, ResolveResult

_DEFAULT_FALLBACK_CHARACTER = "generic_crit"
_MAX_WEIGHT_PER_SLOT = 3.0
_FULL_SUBSTAT_SLOTS = 5
_QUALITY_MAX_SCORE = 25.0
_VALUE_MAX_SCORE = 25.0


class EchoScorer:
    """声骸评分器。双指标制：词条种类分(0-25) + 词条数值分(0-25)，合计满分 50。

    评级阈值与权重表从 weights.json 加载，支持原子热重载。
    """

    def __init__(
        self,
        weights_path: Path,
        aliases_path: Path | None = None,
        default_character: str = _DEFAULT_FALLBACK_CHARACTER,
    ):
        self.weights_path = Path(weights_path)
        self.aliases_path = (
            Path(aliases_path) if aliases_path else (self.weights_path.parent / "aliases.json")
        )
        self.default_character = (default_character or _DEFAULT_FALLBACK_CHARACTER).strip()
        self._raw: dict = {}
        self._sorted_thresholds: list[tuple[str, float]] = []
        self.resolver = CharacterResolver(self.aliases_path)
        self.load()

    def load(self) -> None:
        """从 weights.json 与 aliases.json 加载配置并原子替换内存状态。"""
        with open(self.weights_path, encoding="utf-8-sig") as f:
            raw = json.load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"权重表格式错误(期望 JSON 对象): {self.weights_path}")
        for required_key in ("max_values", "rank_thresholds", "characters"):
            if not isinstance(raw.get(required_key), dict):
                raise ValueError(f"权重表缺少合法的 '{required_key}' 字段: {self.weights_path}")

        sorted_thresholds = sorted(
            (
                (str(rank), float(threshold))
                for rank, threshold in raw["rank_thresholds"].items()
                if isinstance(threshold, (int, float))
                and not isinstance(threshold, bool)
                and math.isfinite(float(threshold))
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )

        # 先尝试加载别名表，若失败不会破坏当前已加载的权重数据
        self.resolver.load()
        self._raw = raw
        self._sorted_thresholds = sorted_thresholds

    def resolve(self, query: str) -> ResolveResult | None:
        """解析角色查询串为规范名。"""
        return self.resolver.resolve(query)

    @property
    def max_values(self) -> dict[str, float]:
        """返回副词条满值字典。"""
        return self._raw.get("max_values", {})

    @property
    def rank_thresholds(self) -> dict[str, float]:
        """返回评级阈值字典。"""
        return self._raw.get("rank_thresholds", {})

    def get_weights(self, character: str) -> dict[str, float]:
        """获取指定角色的副词条权重映射，缺失时自动解析别名并回落默认模板。"""
        chars = self._raw.get("characters", {})
        if not isinstance(chars, dict) or not chars:
            return {}

        target = (character or "").strip()
        if target not in chars:
            result = self.resolver.resolve(target)
            if result and result.canonical in chars:
                target = result.canonical
            else:
                target = self.default_character
        if target not in chars:
            target = _DEFAULT_FALLBACK_CHARACTER
        if target not in chars:
            return {}

        # 解引用 _alias_of，支持多跳并防护循环引用与悬空引用
        visited: set[str] = {target}
        entry = chars[target]
        while isinstance(entry, dict) and "_alias_of" in entry:
            alias_target = entry.get("_alias_of")
            if (
                not isinstance(alias_target, str)
                or alias_target in visited
                or alias_target not in chars
            ):
                fallback = chars.get(_DEFAULT_FALLBACK_CHARACTER, {})
                entry = fallback if isinstance(fallback, dict) else {}
                break
            visited.add(alias_target)
            entry = chars[alias_target]

        if not isinstance(entry, dict):
            return {}

        cleaned: dict[str, float] = {}
        for key, raw_weight in entry.items():
            if key.startswith("_"):
                continue
            if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
                continue
            w = float(raw_weight)
            if not math.isfinite(w):
                continue
            cleaned[key] = max(0.0, w)
        return cleaned

    def rank_for(self, score: float) -> str:
        """按阈值降序计算分数对应的评级（ACE/SSS/SS/S/A/N）。"""
        if not math.isfinite(score):
            return "N"
        # 若调用方直接修改了 _raw["rank_thresholds"]，与缓存长度不同则按实时表排序
        thresholds = self._sorted_thresholds
        live = self.rank_thresholds
        if len(thresholds) != len(live):
            thresholds = sorted(
                ((str(r), float(t)) for r, t in live.items()),
                key=lambda kv: kv[1],
                reverse=True,
            )
        for rank, threshold in thresholds:
            if score >= threshold:
                return rank
        return "N"

    def score_echo(self, echo: Echo, character: str) -> ScoreResult:
        """计算单件声骸的综合评分、分项得分与副词条明细。"""
        weights = self.get_weights(character)
        max_values = self.max_values

        breakdown: list[SubStatBreakdown] = []
        weighted_value_sum = 0.0
        weighted_value_max = 0.0

        for sub in echo.sub_stats:
            w = max(0.0, float(weights.get(sub.name, 0.0)))
            raw_mv = max_values.get(sub.name, 0.0)
            mv = float(raw_mv) if isinstance(raw_mv, (int, float)) and not isinstance(raw_mv, bool) else 0.0
            if not math.isfinite(mv) or mv <= 0:
                breakdown.append(
                    SubStatBreakdown(
                        name=sub.name,
                        value=sub.value,
                        weight=w,
                        score=0.0,
                        note="未知词条" if w == 0 else "缺少 max_value",
                    )
                )
                continue

            # 钳制到 [0.0, 1.0]，防止负值或超标值扭曲得分
            ratio = max(0.0, min(sub.value / mv, 1.0)) if math.isfinite(sub.value) else 0.0
            item_score = ratio * w
            breakdown.append(
                SubStatBreakdown(
                    name=sub.name,
                    value=sub.value,
                    weight=w,
                    score=round(item_score, 3),
                    note=f"{round(ratio * 100, 1)}% 强度",
                )
            )

            if w > 0:
                weighted_value_sum += item_score
                weighted_value_max += w

        quality_raw = self._quality_score(echo, weights)
        if weighted_value_max > 0:
            value_raw = max(
                0.0,
                min((weighted_value_sum / weighted_value_max) * _VALUE_MAX_SCORE, _VALUE_MAX_SCORE),
            )
        else:
            value_raw = 0.0

        # 分别四舍五入后再求和，确保展示分项相加与总分完全一致（无浮点漂移）
        quality_rounded = round(quality_raw, 2)
        value_rounded = round(value_raw, 2)
        total = round(quality_rounded + value_rounded, 2)
        rank = self.rank_for(total)

        return ScoreResult(
            score=total,
            rank=rank,
            quality_score=quality_rounded,
            value_score=value_rounded,
            breakdown=breakdown,
            comment=self._comment_for(total, rank, breakdown),
        )

    def _quality_score(self, echo: Echo, weights: dict[str, float]) -> float:
        """计算词条种类得分（0-25）。

        依据每个副词条的权重等级(3/2/1/0)加权，归一化到 0-25 分。
        理想情况是 5 条均为权重 3 的核心词条（满潜力 15）。
        """
        if not echo.sub_stats:
            return 0.0
        total = sum(max(0.0, float(weights.get(s.name, 0.0))) for s in echo.sub_stats)
        full_potential = _MAX_WEIGHT_PER_SLOT * _FULL_SUBSTAT_SLOTS
        return max(0.0, min((total / full_potential) * _QUALITY_MAX_SCORE, _QUALITY_MAX_SCORE))

    def _comment_for(
        self, score: float, rank: str, breakdown: list[SubStatBreakdown]
    ) -> str:
        """根据评级与有效词条数量生成简短中文评价。"""
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
        """对整套声骸逐件评分并输出总分、均分、短板定位与整体建议。"""
        items = [self.score_echo(e, character) for e in echo_set.echoes]
        if not items:
            return SetScoreResult(total_score=0.0, average_score=0.0, rank="N", items=[])

        total_rounded = round(sum(i.score for i in items), 2)
        avg_rounded = round(total_rounded / len(items), 2)
        # 按展示均分计算评级，避免出现"均 35/50 却评 SS"的边界不一致
        rank = self.rank_for(avg_rounded)
        weakest_idx = min(range(len(items)), key=lambda i: items[i].score)

        comment = self._set_comment(items, weakest_idx)
        return SetScoreResult(
            total_score=total_rounded,
            average_score=avg_rounded,
            rank=rank,
            items=items,
            weakest_index=weakest_idx,
            comment=comment,
        )

    def _set_comment(self, items: list[ScoreResult], weakest_idx: int) -> str:
        """生成整套声骸短板提示或养卡建议。"""
        if len(items) < _FULL_SUBSTAT_SLOTS:
            return f"只识别到 {len(items)} 件声骸,完整一套需要 5 件。"
        weak = items[weakest_idx]
        if weak.score < 18:
            return f"第 {weakest_idx + 1} 件是短板({weak.score:g} 分,{weak.rank}),优先替换它。"
        return "整体均衡,继续养。"
