from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class SubStat(BaseModel):
    name: str = Field(..., description="副词条名,如 '暴击' / '暴击伤害' / '攻击%'")
    value: float = Field(..., description="副词条数值(百分比直接写小数前的数字,如 9.3 表示 9.3%)")


class Echo(BaseModel):
    cost: int = Field(..., description="声骸 COST(1/3/4)")
    set_name: Optional[str] = Field(None, description="所属合鸣效果套装名,如 '凝夜白霜'")
    main_stat: Optional[str] = Field(None, description="主词条(COST 4/3 才有,COST 1 没有)")
    main_stat_value: Optional[float] = None
    sub_stats: List[SubStat] = Field(default_factory=list, description="副词条,通常 1-5 条")
    level: int = Field(0, description="强化等级 0-25")


class EchoSet(BaseModel):
    """一套 5 件声骸(1 个 COST4 + 1 个 COST3 + 3 个 COST1)"""
    echoes: List[Echo] = Field(default_factory=list)


class SubStatBreakdown(BaseModel):
    name: str
    value: float
    weight: float
    score: float
    note: str = ""


class ScoreResult(BaseModel):
    score: float = Field(..., description="0-50 总分")
    rank: str = Field(..., description="ACE/SSS/SS/S/A/N")
    quality_score: float = Field(..., description="0-25 词条种类得分")
    value_score: float = Field(..., description="0-25 词条数值得分")
    breakdown: List[SubStatBreakdown] = Field(default_factory=list)
    comment: str = Field("", description="简短文字评价")


class SetScoreResult(BaseModel):
    total_score: float
    average_score: float
    rank: str
    items: List[ScoreResult] = Field(default_factory=list)
    weakest_index: int = -1
    comment: str = ""
