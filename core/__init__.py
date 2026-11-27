"""鸣潮声骸评分插件核心模块。

包含数据模型、角色别名解析、多模态 OCR 清洗校验、评分引擎与上游数据同步。
"""

from __future__ import annotations

from .models import (
    Echo,
    EchoSet,
    ScoreResult,
    SetScoreResult,
    SubStat,
    SubStatBreakdown,
)
from .resolver import CharacterResolver, ResolveResult
from .scorer import EchoScorer

__all__ = [
    "CharacterResolver",
    "Echo",
    "EchoScorer",
    "EchoSet",
    "ResolveResult",
    "ScoreResult",
    "SetScoreResult",
    "SubStat",
    "SubStatBreakdown",
]
