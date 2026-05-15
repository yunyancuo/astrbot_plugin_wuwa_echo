from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional


class ResolveResult(NamedTuple):
    canonical: str
    matched_term: str
    kind: str  # 'exact' | 'alias' | 'substring' | 'fuzzy'


class CharacterResolver:
    """把用户输入的角色名/昵称解析为规范名。

    四级回落:
      1. exact     原样命中规范名
      2. alias     不区分大小写命中任一别名
      3. substring 双向子串匹配(查询包含/被包含),仅在唯一命中时采纳
      4. fuzzy     difflib 相似度匹配(cutoff=0.6)
    """

    FUZZY_CUTOFF = 0.6

    def __init__(self, aliases_path: Path):
        self.aliases_path = aliases_path
        self._aliases: Dict[str, List[str]] = {}
        self._reverse: Dict[str, str] = {}
        self._terms_lower: List[str] = []
        self.load()

    def load(self) -> None:
        if not self.aliases_path.exists():
            self._aliases = {}
            self._reverse = {}
            self._terms_lower = []
            return
        with open(self.aliases_path, "r", encoding="utf-8-sig") as f:
            self._aliases = json.load(f)

        self._reverse = {}
        for canonical, alias_list in self._aliases.items():
            self._reverse[canonical.lower()] = canonical
            for alias in alias_list:
                # 后写覆盖:同名别名优先保留先出现的角色
                self._reverse.setdefault(alias.lower(), canonical)
        self._terms_lower = list(self._reverse.keys())

    def canonical_names(self) -> List[str]:
        return list(self._aliases.keys())

    def aliases_of(self, canonical: str) -> List[str]:
        return list(self._aliases.get(canonical, []))

    def resolve(self, query: str) -> Optional[ResolveResult]:
        if not query:
            return None
        q = query.strip()
        if not q:
            return None
        ql = q.lower()

        if q in self._aliases:
            return ResolveResult(q, q, "exact")

        if ql in self._reverse:
            canonical = self._reverse[ql]
            return ResolveResult(canonical, q, "alias")

        if len(q) >= 2:
            hits = {self._reverse[t] for t in self._terms_lower if ql in t}
            if len(hits) == 1:
                canonical = next(iter(hits))
                return ResolveResult(canonical, q, "substring")

        candidates = difflib.get_close_matches(
            ql, self._terms_lower, n=1, cutoff=self.FUZZY_CUTOFF
        )
        if candidates:
            return ResolveResult(self._reverse[candidates[0]], q, "fuzzy")

        return None
