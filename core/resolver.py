"""角色名、别名与自由文本角色提取解析器。"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from pathlib import Path
from re import Pattern
from typing import NamedTuple

# 角色名中常见分隔符（中点、全角点、空格、连接号等）
_SEP_RE = re.compile(r"[\s·・•‧._/\-]+")
# 纯 ASCII 字母数字词条（匹配时需施加词边界，避免英文单词内部误命中）
_ASCII_WORD_RE = re.compile(r"^[a-z0-9]+$")
# 汉字字符区间，用于约束单字角色名（如"心"、"椿"）在长句中的防误触检查
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
# 单字角色名前后允许紧邻的口语虚词（如"给椿评分"、"椿的声骸"、"帮心打分"）
_SINGLE_CHAR_PARTICLES = frozenset("给帮为的用带看这那问查测下把对和跟")


def normalize_term(text: str) -> str:
    """对输入角色查询串做紧凑归一化（去除空白与常见中点/标点并转小写）。

    示例:
        - "陆·赫斯" / "陆.赫斯" / "陆 赫斯" -> "陆赫斯"
        - "Lucyna " -> "lucyna"
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).strip().lower()
    return _SEP_RE.sub("", s)


def _compile_term_pattern(term_lower: str) -> Pattern[str] | None:
    """为自由文本角色提取编译匹配正则。

    规则:
      - 纯 ASCII 字母数字词条强制添加 (?<![a-z0-9]) 与 (?![a-z0-9]) 词边界，
        防止 "class" 误命中 "cl"/"ss" 或 "damage" 误命中 "ag"，
        同时仍允许 "jx评分"、"评分kkl" 等中英紧邻写法命中。
      - 含中点/分隔符的词条（如 "秧秧·玄翎"、"陆·赫斯"）允许任意中点/空格/无间隔匹配。
      - 普通中文词条返回 None，直接使用更快的子串查找。
    """
    if _ASCII_WORD_RE.match(term_lower):
        return re.compile(rf"(?<![a-z0-9]){re.escape(term_lower)}(?![a-z0-9])")
    if _SEP_RE.search(term_lower):
        parts = [re.escape(p) for p in _SEP_RE.split(term_lower) if p]
        if len(parts) >= 2:
            joined = r"[\s·・•‧._/\-]*".join(parts)
            if _ASCII_WORD_RE.match("".join(_SEP_RE.split(term_lower))):
                return re.compile(rf"(?<![a-z0-9]){joined}(?![a-z0-9])")
            return re.compile(joined)
    return None


def _matches_single_char_canonical(char: str, text: str) -> bool:
    """检查单字中文规范名（如"椿"、"心"）是否以合理语境出现在文本中。

    避免 "这声骸真让人伤心" 等普通汉语词汇误命中单字角色名，
    同时保留 "椿评分"、"给椿打分"、"椿的声骸"、"帮我看看 椿" 等自然表达。
    """
    start = 0
    text_len = len(text)
    while True:
        idx = text.find(char, start)
        if idx < 0:
            return False
        prev_ch = text[idx - 1] if idx > 0 else ""
        next_ch = text[idx + 1] if idx + 1 < text_len else ""
        prev_ok = (
            not prev_ch
            or not _CJK_CHAR_RE.match(prev_ch)
            or prev_ch in _SINGLE_CHAR_PARTICLES
        )
        next_ok = (
            not next_ch
            or not _CJK_CHAR_RE.match(next_ch)
            or next_ch in _SINGLE_CHAR_PARTICLES
        )
        if prev_ok and next_ok:
            return True
        start = idx + 1


class ResolveResult(NamedTuple):
    """角色解析结果。"""

    canonical: str
    matched_term: str
    kind: str  # 'exact' | 'alias' | 'substring' | 'fuzzy'


class CharacterResolver:
    """把用户输入的角色名/昵称解析为规范名。

    四级回落:
      1. exact     原样命中规范名
      2. alias     不区分大小写或标点/中点归一化后命中任一别名
      3. substring 双向子串匹配(查询被词条包含，或查询包含长度>=2词条)，仅在唯一命中时采纳
      4. fuzzy     difflib 相似度匹配(cutoff=0.6)
    """

    FUZZY_CUTOFF = 0.6

    def __init__(self, aliases_path: Path):
        self.aliases_path = Path(aliases_path)
        self._aliases: dict[str, list[str]] = {}
        self._reverse: dict[str, str] = {}
        self._normalized_reverse: dict[str, str] = {}
        self._terms_lower: list[str] = []
        # 自由文本提取预构建索引（均按词长降序排列，避免短名遮蔽长名）
        self._canonical_matchers: list[tuple[str, str, Pattern[str] | None]] = []
        self._alias_matchers: list[tuple[str, str, Pattern[str] | None]] = []
        self.load()

    def load(self) -> None:
        """从 aliases.json 加载别名表并原子切换内存索引。

        若文件不存在则加载为空表；若文件损坏或结构非法则抛出 ValueError，
        确保热重载失败时旧的内存索引保持可用不被破坏。
        """
        if not self.aliases_path.exists():
            self._aliases = {}
            self._reverse = {}
            self._normalized_reverse = {}
            self._terms_lower = []
            self._canonical_matchers = []
            self._alias_matchers = []
            return

        with open(self.aliases_path, encoding="utf-8-sig") as f:
            raw = json.load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"别名表格式错误(期望 JSON 对象): {self.aliases_path}")

        aliases: dict[str, list[str]] = {}
        reverse: dict[str, str] = {}
        normalized_reverse: dict[str, str] = {}

        # 第一遍先注册全部规范名，确保规范名优先级永远高于跨角色冲突别名
        for raw_canonical, raw_list in raw.items():
            if not isinstance(raw_canonical, str):
                continue
            canonical = raw_canonical.strip()
            if not canonical:
                continue
            cleaned_list: list[str] = []
            seen_lower: set[str] = set()
            if isinstance(raw_list, list):
                for item in raw_list:
                    if not isinstance(item, str):
                        continue
                    term = item.strip()
                    if not term:
                        continue
                    tl = term.lower()
                    if tl in seen_lower:
                        continue
                    seen_lower.add(tl)
                    cleaned_list.append(term)
            aliases[canonical] = cleaned_list
            reverse[canonical.lower()] = canonical
            norm_c = normalize_term(canonical)
            if norm_c:
                normalized_reverse.setdefault(norm_c, canonical)

        # 第二遍注册别名（冲突别名保留先出现的角色，保持向后兼容）
        for canonical, alias_list in aliases.items():
            for alias in alias_list:
                al = alias.lower()
                reverse.setdefault(al, canonical)
                norm_a = normalize_term(alias)
                if norm_a:
                    normalized_reverse.setdefault(norm_a, canonical)

        # 构建自由文本提取索引：按词长降序，解决"秧秧"遮蔽"秧秧·玄翎"等问题
        canonical_matchers: list[tuple[str, str, Pattern[str] | None]] = []
        for canonical in sorted(aliases.keys(), key=lambda s: (-len(s), s)):
            cl = canonical.lower()
            canonical_matchers.append((canonical, cl, _compile_term_pattern(cl)))

        alias_entries: list[tuple[str, str]] = []
        seen_alias_lower: set[str] = set()
        for canonical, alias_list in aliases.items():
            for alias in alias_list:
                al = alias.lower()
                if len(alias) < 2 or al == canonical.lower() or al in seen_alias_lower:
                    continue
                # 跨角色冲突别名仅归属 reverse 中记录的首个角色
                if reverse.get(al) != canonical:
                    continue
                seen_alias_lower.add(al)
                alias_entries.append((canonical, al))

        alias_entries.sort(key=lambda pair: (-len(pair[1]), pair[1]))
        alias_matchers = [
            (canonical, al, _compile_term_pattern(al)) for canonical, al in alias_entries
        ]

        # 原子切换全部引用
        self._aliases = aliases
        self._reverse = reverse
        self._normalized_reverse = normalized_reverse
        self._terms_lower = list(reverse.keys())
        self._canonical_matchers = canonical_matchers
        self._alias_matchers = alias_matchers

    def canonical_names(self) -> list[str]:
        """返回所有规范角色名列表。"""
        return list(self._aliases.keys())

    def aliases_of(self, canonical: str) -> list[str]:
        """返回指定规范名的全部别名列表。"""
        return list(self._aliases.get(canonical, []))

    def extract_from_text(self, text: str) -> str:
        """从自由文本中提取最优先匹配的角色规范名，未命中返回空串。

        优先级:
          1. 规范名（按长度降序，支持中点/空格变体；单字名需通过上下文防误触检查）
          2. 长度 >= 2 的别名（按长度降序；纯 ASCII 词条强制词边界匹配）
        """
        if not text:
            return ""
        lowered = unicodedata.normalize("NFKC", text).lower()
        if not lowered.strip():
            return ""

        for canonical, cl, pattern in self._canonical_matchers:
            if pattern is not None:
                if pattern.search(lowered):
                    return canonical
            elif len(cl) == 1:
                if _matches_single_char_canonical(cl, lowered):
                    return canonical
            elif cl in lowered:
                return canonical

        for canonical, al, pattern in self._alias_matchers:
            if pattern is not None:
                if pattern.search(lowered):
                    return canonical
            elif al in lowered:
                return canonical

        return ""

    def resolve(self, query: str) -> ResolveResult | None:
        """按四级回落策略将角色查询串解析为规范名。"""
        if not query or not isinstance(query, str):
            return None
        q = query.strip()
        if not q:
            return None
        ql = q.lower()

        # 1. exact：原样命中规范名
        if q in self._aliases:
            return ResolveResult(q, q, "exact")

        # 2. alias：不区分大小写或归一化命中规范名/别名
        if ql in self._reverse:
            canonical = self._reverse[ql]
            return ResolveResult(canonical, q, "alias")

        norm_q = normalize_term(q)
        if norm_q and norm_q in self._normalized_reverse:
            canonical = self._normalized_reverse[norm_q]
            return ResolveResult(canonical, q, "alias")

        # 3. substring：双向子串匹配（仅在唯一命中时采纳）
        if len(q) >= 2:
            # 3a. 查询被词条包含（如"卡提希" -> "卡提希娅"）
            hits = {self._reverse[t] for t in self._terms_lower if ql in t}
            if not hits and len(norm_q) >= 2:
                hits = {
                    c for norm_t, c in self._normalized_reverse.items() if norm_q in norm_t
                }
            if len(hits) == 1:
                return ResolveResult(next(iter(hits)), q, "substring")

            # 3b. 查询包含词条（如"今汐声骸" -> "今汐"，按最长匹配筛选）
            contained_matches: list[tuple[int, str]] = []
            for canonical, cl, pattern in self._canonical_matchers:
                if len(cl) < 2:
                    continue
                matched = bool(pattern.search(ql)) if pattern is not None else (cl in ql)
                if matched:
                    contained_matches.append((len(cl), canonical))
            for canonical, al, pattern in self._alias_matchers:
                matched = bool(pattern.search(ql)) if pattern is not None else (al in ql)
                if matched:
                    contained_matches.append((len(al), canonical))
            if contained_matches:
                max_len = max(length for length, _ in contained_matches)
                top_hits = {c for length, c in contained_matches if length == max_len}
                if len(top_hits) == 1:
                    return ResolveResult(next(iter(top_hits)), q, "substring")

        # 4. fuzzy：difflib 相似度匹配
        candidates = difflib.get_close_matches(
            ql, self._terms_lower, n=1, cutoff=self.FUZZY_CUTOFF
        )
        if candidates:
            return ResolveResult(self._reverse[candidates[0]], q, "fuzzy")

        return None
