"""XutheringWavesUID-Resources 数据自动同步。

数据链: 库街区/社区 → XutheringWavesUID-Resources (CNB 仓库, 跟随版本更新)
        → 本模块拉取并生成 → data/weights.json + data/aliases.json

自动做的事:
  1. 拉取角色别名表 (char_alias.json, 60+ 角色含最新版本)
  2. 拉取 CharId2Data.json 得到 id -> 角色名
  3. 对本地 weights.json 中没有的角色, 拉取其 calc.json 评分权重,
     映射为插件的 0-3 档权重后合并 (绝不覆盖手工调过的已有条目)
  4. 别名表做并集合并 (保留本地自定义别名)

纯标准库实现, 不依赖 astrbot, 可独立运行:
    python core/sync.py --data-dir data          # 同步并写入
    python core/sync.py --data-dir data --check  # 只看会变什么, 不写入
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# 资源源, 按优先级排列; CNB 为国内直连主源
RESOURCE_BASES = [
    "https://cnb.cool/loping151/XutheringWavesUID-Resources/-/git/raw/main/XutheringWavesUID/resource/",
    "https://cdn.jsdelivr.net/gh/Loping151/XutheringWavesUID@main/XutheringWavesUID/resource/",
    "https://raw.githubusercontent.com/Loping151/XutheringWavesUID/main/XutheringWavesUID/resource/",
]

ALIAS_PATH = "map/alias/char_alias.json"
ID2NAME_PATH = "map/CharId2Data.json"
CALC_PATH = "map/character/{id}/calc.json"

FETCH_TIMEOUT = 30.0
RETRY_PER_SOURCE = 2  # 每源重试次数; CNB raw 并发下偶发误报 404(限流), 退避重试即可
RETRY_BACKOFF = 0.8

# 插件标准 13 副词条名 (与 ocr.py SUBSTAT_MAX_VALUE 一致)
STANDARD_SUBSTATS = [
    "暴击", "暴击伤害", "攻击%", "生命%", "防御%", "共鸣效率",
    "普攻伤害加成", "重击伤害加成", "共鸣技能伤害加成", "共鸣解放伤害加成",
    "攻击", "生命", "防御",
]

# calc.json 使用的词条名 -> 插件标准名; 不在表内也不在标准名里的词条忽略
CALC_NAME_MAP = {
    "技能伤害加成": "共鸣技能伤害加成",
}

# 相对权重分档阈值 (相对该角色最高权重值)
BUCKET_HIGH = 0.75  # >= 3
BUCKET_MID = 0.40  # >= 2
BUCKET_LOW = 0.12  # >= 1


@dataclass
class SyncReport:
    new_characters: List[str] = field(default_factory=list)
    alias_canonicals_added: List[str] = field(default_factory=list)
    alias_terms_added: int = 0
    unmapped_substats: Dict[str, List[str]] = field(default_factory=dict)
    missing_calc: List[str] = field(default_factory=list)  # 上游还没有 calc 数据(新角色常见)
    errors: List[str] = field(default_factory=list)
    weights_changed: bool = False
    aliases_changed: bool = False

    def summary(self) -> str:
        parts = []
        if self.new_characters:
            parts.append(f"新增角色权重 {len(self.new_characters)} 个: {'、'.join(self.new_characters[:8])}")
        if self.missing_calc:
            parts.append(f"暂无评分数据(等上游更新): {'、'.join(self.missing_calc[:6])}")
        if self.alias_canonicals_added:
            parts.append(f"新增角色别名 {len(self.alias_canonicals_added)} 组")
        if self.alias_terms_added:
            parts.append(f"合并别名词条 {self.alias_terms_added} 个")
        if self.unmapped_substats:
            parts.append(f"未识别词条 {sum(len(v) for v in self.unmapped_substats.values())} 个")
        if not parts:
            parts.append("数据已是最新")
        if self.errors:
            parts.append(f"错误 {len(self.errors)} 条")
        return "; ".join(parts)


class ResourceNotFound(RuntimeError):
    """所有源都返回 404, 说明上游暂时没有这个文件(新角色 calc 常见)。"""


def _fetch_url(url: str, timeout: float = FETCH_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "astrbot-wuwa-echo/sync"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


async def fetch_resource(path: str, timeout: float = FETCH_TIMEOUT) -> bytes:
    """按源优先级拉取资源文件, 每源带重试与退避。

    主源(CNB)重试后仍 404 视为上游确实没有该文件, 抛 ResourceNotFound;
    其他网络异常抛 RuntimeError。
    """
    last_exc: Optional[Exception] = None
    primary_404_count = 0
    for si, base in enumerate(RESOURCE_BASES):
        for attempt in range(RETRY_PER_SOURCE):
            try:
                return await asyncio.to_thread(_fetch_url, base + path, timeout)
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code == 404:
                    if si == 0:
                        primary_404_count += 1
                    # 退避后同源重试(限流误报通常重试即成功)
                    await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                else:
                    break
            except Exception as e:  # noqa: BLE001 - 网络异常种类多, 逐源回落
                last_exc = e
                break
    if primary_404_count >= RETRY_PER_SOURCE:
        raise ResourceNotFound(path)
    raise RuntimeError(f"所有资源源拉取 {path} 失败: {last_exc}")


def _load_json_bytes(raw: bytes):
    return json.loads(raw.decode("utf-8-sig"))


def calc_to_weights(calc: dict) -> tuple[Dict[str, int], Dict[str, float], List[str]]:
    """把 calc.json 的连续权重 (0-2 标度) 映射为插件 0-3 档权重。

    分档以该角色最高有效权重为基准做相对映射, 与手工模板的语义对齐
    (最高档≈核心词条, 中档≈重要词条)。
    返回 (0-3 权重, 原始权重, 未识别词条名列表)。
    """
    sub_props = calc.get("sub_props") or {}
    mapped: Dict[str, float] = {}
    unmapped: List[str] = []
    for name, value in sub_props.items():
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        std = name if name in STANDARD_SUBSTATS else CALC_NAME_MAP.get(name)
        if std is None:
            if v > 0:
                unmapped.append(name)
            continue
        mapped[std] = max(mapped.get(std, 0.0), v)

    if not mapped:
        return {}, {}, unmapped

    peak = max(mapped.values())
    weights: Dict[str, int] = {}
    for std in STANDARD_SUBSTATS:
        v = mapped.get(std, 0.0)
        if peak <= 0 or v < BUCKET_LOW * peak:
            weights[std] = 0
        elif v >= BUCKET_HIGH * peak:
            weights[std] = 3
        elif v >= BUCKET_MID * peak:
            weights[std] = 2
        else:
            weights[std] = 1
    return weights, mapped, unmapped


def merge_aliases(local: Dict[str, List[str]], remote: Dict[str, List[str]]) -> tuple[Dict[str, List[str]], SyncReport]:
    """别名并集合并; 本地条目(含自定义昵称)永远保留。"""
    report = SyncReport()
    merged = {k: list(v) for k, v in local.items()}
    for canonical, terms in remote.items():
        if not isinstance(terms, list):
            continue
        if canonical not in merged:
            merged[canonical] = list(dict.fromkeys(terms))
            report.alias_canonicals_added.append(canonical)
            continue
        existing = set(merged[canonical])
        fresh = [t for t in terms if t and t not in existing]
        if fresh:
            merged[canonical].extend(fresh)
            report.alias_terms_added += len(fresh)
    return merged, report


def _load_local_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        path.replace(bak)
    tmp.replace(path)


async def sync_data(
    data_dir: Path,
    *,
    write: bool = True,
    fetch_existing_calcs: bool = False,
) -> SyncReport:
    """执行一次同步。write=False 时只算差异不落盘 (--check 模式)。

    fetch_existing_calcs=True 时对已有角色也重新拉 calc (用于重建权重表)。
    """
    data_dir = Path(data_dir)
    report = SyncReport()

    weights_file = data_dir / "weights.json"
    aliases_file = data_dir / "aliases.json"
    local_weights = _load_local_json(weights_file, {})
    local_aliases = _load_local_json(aliases_file, {})
    characters: Dict = dict(local_weights.get("characters") or {})

    # 1. 拉别名表与 id->name
    try:
        remote_aliases = _load_json_bytes(await fetch_resource(ALIAS_PATH))
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"别名表拉取失败: {e}")
        remote_aliases = {}
    try:
        id2name = _load_json_bytes(await fetch_resource(ID2NAME_PATH))
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"CharId2Data 拉取失败: {e}")
        id2name = {}

    # 2. 算出需要生成权重的角色 (本地没有的)
    existing = set(characters.keys())
    remote_names = {v.get("name") for v in id2name.values() if isinstance(v, dict) and v.get("name")}
    remote_names |= set(remote_aliases.keys()) if isinstance(remote_aliases, dict) else set()
    todo_ids = [
        cid
        for cid, info in id2name.items()
        if isinstance(info, dict)
        and info.get("name")
        and (info["name"] not in existing or fetch_existing_calcs)
    ]
    # 同名多 id (主角男/女形态等) 只取第一个
    seen_names: set = set()
    dedup_ids = []
    for cid in todo_ids:
        name = id2name[cid]["name"]
        if name in seen_names:
            continue
        seen_names.add(name)
        dedup_ids.append(cid)

    # 3. 并发拉 calc.json 并生成权重 (并发 4: CNB raw 高并发会限流)
    sem = asyncio.Semaphore(4)

    async def _fetch_calc(cid: str):
        async with sem:
            try:
                return cid, _load_json_bytes(await fetch_resource(CALC_PATH.format(id=cid))), False
            except ResourceNotFound:
                # 新角色上游还没生成评分数据, 软跳过, 下次同步再试
                return cid, None, True
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"{id2name[cid].get('name')} calc 拉取失败: {e}")
                return cid, None, False

    results = await asyncio.gather(*[_fetch_calc(cid) for cid in dedup_ids])
    generated: Dict[str, Dict] = {}
    for cid, calc, missing in results:
        if missing:
            report.missing_calc.append(id2name[cid].get("name", cid))
            continue
        if not calc:
            continue
        name = id2name[cid]["name"]
        weights, raw, unmapped = calc_to_weights(calc)
        if unmapped:
            report.unmapped_substats[name] = unmapped
        if not weights:
            report.errors.append(f"{name} calc 无有效权重, 跳过")
            continue
        desc_bits = ", ".join(f"{k}{v:g}" for k, v in sorted(raw.items(), key=lambda kv: -kv[1])[:4])
        generated[name] = {
            "_desc": f"自动生成 (XutheringWavesUID calc: {calc.get('name', name)}; 主要词条 {desc_bits})",
            **weights,
        }

    new_chars = {k: v for k, v in generated.items() if k not in existing}
    if new_chars:
        report.new_characters = sorted(new_chars)
        characters.update(new_chars)

    # 4. 别名合并
    merged_aliases, alias_report = merge_aliases(local_aliases, remote_aliases)
    report.alias_canonicals_added = alias_report.alias_canonicals_added
    report.alias_terms_added = alias_report.alias_terms_added

    # 5. 落盘 (仅在有变化时)
    if write and new_chars:
        local_weights["characters"] = characters
        _atomic_write_json(weights_file, local_weights)
        report.weights_changed = True
    if write and (report.alias_canonicals_added or report.alias_terms_added):
        _atomic_write_json(aliases_file, merged_aliases)
        report.aliases_changed = True

    return report


def main(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="鸣潮声骸插件数据同步")
    parser.add_argument("--data-dir", default=str(Path(__file__).parent.parent / "data"))
    parser.add_argument("--check", action="store_true", help="只检查不写入")
    args = parser.parse_args(argv)

    async def _run():
        report = await sync_data(
            Path(args.data_dir), write=not args.check, fetch_existing_calcs=False
        )
        print(report.summary())
        for err in report.errors:
            print(f"  [warn] {err}")
        return 0 if not report.errors else 1

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
