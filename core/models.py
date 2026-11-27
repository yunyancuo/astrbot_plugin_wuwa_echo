"""声骸识别与评分核心数据模型。"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field, field_validator


class SubStat(BaseModel):
    """单条声骸副词条模型。"""

    name: str = Field(..., description="副词条名,如 '暴击' / '暴击伤害' / '攻击%'")
    value: float = Field(
        ...,
        description="副词条数值(百分比直接写小数前的数字,如 9.3 表示 9.3%)",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        """去除副词条名首尾空白。"""
        return v.strip()

    @field_validator("value")
    @classmethod
    def _validate_finite_value(cls, v: float) -> float:
        """拦截 NaN 与无穷大，防止污染后续评分计算。"""
        if not math.isfinite(v):
            raise ValueError("副词条数值必须为有限实数")
        return v


class Echo(BaseModel):
    """单件声骸模型。"""

    cost: int = Field(..., description="声骸 COST(1/3/4)")
    set_name: str | None = Field(None, description="所属合鸣效果套装名,如 '凝夜白霜'")
    main_stat: str | None = Field(None, description="主词条(COST 4/3 才有,COST 1 没有)")
    main_stat_value: float | None = Field(None, description="主词条数值")
    sub_stats: list[SubStat] = Field(default_factory=list, description="副词条,通常 1-5 条")
    level: int = Field(0, description="强化等级 0-25")

    @field_validator("main_stat_value")
    @classmethod
    def _validate_main_stat_value(cls, v: float | None) -> float | None:
        """过滤非有限主词条数值。"""
        if v is not None and not math.isfinite(v):
            return None
        return v


class EchoSet(BaseModel):
    """一套声骸模型(通常为 5 件：1 个 COST4 + 1 个 COST3 + 3 个 COST1 或 43311)。"""

    echoes: list[Echo] = Field(default_factory=list)


class SubStatBreakdown(BaseModel):
    """单条副词条的评分明细。"""

    name: str
    value: float
    weight: float
    score: float
    note: str = ""


class ScoreResult(BaseModel):
    """单件声骸评分结果。"""

    score: float = Field(..., description="0-50 总分")
    rank: str = Field(..., description="ACE/SSS/SS/S/A/N")
    quality_score: float = Field(..., description="0-25 词条种类得分")
    value_score: float = Field(..., description="0-25 词条数值得分")
    breakdown: list[SubStatBreakdown] = Field(default_factory=list)
    comment: str = Field("", description="简短文字评价")


class SetScoreResult(BaseModel):
    """整套声骸综合评分结果。"""

    total_score: float
    average_score: float
    rank: str
    items: list[ScoreResult] = Field(default_factory=list)
    weakest_index: int = -1
    comment: str = ""


__all__ = [
    "Echo",
    "EchoSet",
    "ScoreResult",
    "SetScoreResult",
    "SubStat",
    "SubStatBreakdown",
]
