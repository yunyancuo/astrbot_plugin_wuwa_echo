"""XutheringWavesUID-Resources 数据自动同步。

数据链: 库街区/社区 → XutheringWavesUID-Resources (CNB 仓库, 跟随版本更新)
        → 本模块拉取并生成 → data/weights.json + data/aliases.json

自动做的事:
  1. 拉取角色别名表 (char_alias.json, 60+ 角色含最新版本)
  2. 拉取 CharId2Data.json 得到 id -> 角色名
  3. 对本地 weights.json 中没有的角色, 拉取其 calc.json 评分权重,
     映射为插件的 0-3 档权重后合并 (绝不覆盖手工调过的已有条目)
  4. 别名表做并集合并 (保留本地自定义别名，并清洗空白与重复项)
  5. 落盘前做完整 JSON Schema 校验 + 原子写入 + 并发锁保护，防止坏数据覆盖本地快照

纯标准库实现, 不依赖 astrbot, 可独立运行:
    python core/sync.py --data-dir data          # 同步并写入
    python core/sync.py --data-dir data --check  # 只看会变什么, 不写入
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import math
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

# 资源源，按优先级排列；CNB 为国内直连主源
RESOURCE_BASES = [
    "https://cnb.cool/loping151/XutheringWavesUID-Resources/-/git/raw/main/XutheringWavesUID/resource/",
    "https://cdn.jsdelivr.net/gh/Loping151/XutheringWavesUID@main/XutheringWavesUID/resource/",
    "https://raw.githubusercontent.com/Loping151/XutheringWavesUID/main/XutheringWavesUID/resource/",
]

# 允许重定向到的补充 CDN 域名（例如 GitHub raw 重定向）
EXTRA_ALLOWED_REDIRECT_HOSTS = frozenset({"objects.githubusercontent.com"})

ALIAS_PATH = "map/alias/char_alias.json"
ID2NAME_PATH = "map/CharId2Data.json"
CALC_PATH = "map/character/{id}/calc.json"

FETCH_TIMEOUT = 30.0
RETRY_PER_SOURCE = 2  # 每源重试次数；CNB raw 并发下偶发误报 404（限流），退避重试即可
RETRY_BACKOFF = 0.8
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 单次远程文件上限 10 MiB，防止 OOM DoS
_RETRYABLE_HTTP_CODES = frozenset({404, 429, 500, 502, 503, 504})

# 插件标准 13 副词条名（与 ocr.py SUBSTAT_MAX_VALUE 一致）
STANDARD_SUBSTATS = [
    "暴击",
    "暴击伤害",
    "攻击%",
    "生命%",
    "防御%",
    "共鸣效率",
    "普攻伤害加成",
    "重击伤害加成",
    "共鸣技能伤害加成",
    "共鸣解放伤害加成",
    "攻击",
    "生命",
    "防御",
]

# calc.json 使用的词条名 -> 插件标准名；不在表内也不在标准名里的词条忽略
CALC_NAME_MAP = {
    "技能伤害加成": "共鸣技能伤害加成",
}

# 相对权重分档阈值（相对该角色最高权重值）
BUCKET_HIGH = 0.75  # >= 3
BUCKET_MID = 0.40  # >= 2
BUCKET_LOW = 0.12  # >= 1

_SAFE_CID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_REL_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")

# 按事件循环隔离的目录级并发同步锁，防止并发同步写盘竞态
_LOOP_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = (
    WeakKeyDictionary()
)


@dataclass
class SyncReport:
    """单次同步执行的汇总报告。"""

    new_characters: list[str] = field(default_factory=list)
    alias_canonicals_added: list[str] = field(default_factory=list)
    alias_terms_added: int = 0
    unmapped_substats: dict[str, list[str]] = field(default_factory=dict)
    missing_calc: list[str] = field(default_factory=list)  # 上游还没有 calc 数据（新角色常见）
    errors: list[str] = field(default_factory=list)
    weights_changed: bool = False
    aliases_changed: bool = False

    def summary(self) -> str:
        """生成面向日志的简短中文摘要。"""
        parts = []
        if self.new_characters:
            parts.append(
                f"新增角色权重 {len(self.new_characters)} 个: {'、'.join(self.new_characters[:8])}"
            )
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
    """所有源都返回 404，说明上游暂时没有这个文件（新角色 calc 常见）。"""


def _allowed_hosts() -> set[str]:
    """根据当前配置的 RESOURCE_BASES 与受信任 CDN 白名单计算允许访问的主机集合。"""
    hosts: set[str] = set(EXTRA_ALLOWED_REDIRECT_HOSTS)
    for base in RESOURCE_BASES:
        parsed = urllib.parse.urlparse(base)
        if parsed.hostname:
            hosts.add(parsed.hostname.lower())
    return hosts


def _validate_url(url: str) -> str:
    """校验远程下载 URL 的安全性，防御 SSRF、非 HTTPS 协议及内网地址访问。"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("远程 URL 不能为空")
    if any(ch in url for ch in ("\r", "\n", "\x00")):
        raise ValueError("远程 URL 包含非法控制字符")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"仅允许 HTTPS 协议拉取同步数据: {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ValueError("远程 URL 不允许携带凭据")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("远程 URL 缺少主机名")

    # 拒绝任何直连 IP（尤其是环回/私网/链路本地元数据地址）
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        raise ValueError(f"不允许直接访问 IP 地址: {host}")

    if host not in _allowed_hosts():
        raise ValueError(f"远程主机不在白名单中: {host}")
    return url


def _validate_relative_path(path: str) -> str:
    """校验上游资源相对路径，防止路径穿越与 URL 注入。"""
    if not isinstance(path, str) or not path:
        raise ValueError("资源路径不能为空")
    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        raise ValueError(f"非法的资源相对路径: {path!r}")
    if not _SAFE_REL_PATH_RE.match(path):
        raise ValueError(f"资源路径包含非法字符: {path!r}")
    return path


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """带 SSRF 防护的 HTTP 重定向处理器，对每次跳转目标执行白名单校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_url(url: str, timeout: float = FETCH_TIMEOUT) -> bytes:
    """同步拉取单个白名单 HTTPS URL，附带重定向校验与响应体大小上限防护。"""
    safe_url = _validate_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(safe_url, headers={"User-Agent": "astrbot-wuwa-echo/sync"})
    with opener.open(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        if final_url:
            _validate_url(final_url)
        data = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError(f"远程响应超过大小上限({MAX_RESPONSE_BYTES} 字节)")
        return data


async def fetch_resource(path: str, timeout: float = FETCH_TIMEOUT) -> bytes:
    """按源优先级拉取资源文件，每源带重试与退避。

    主源（CNB）重试后仍 404 且备用源均失败时视为上游确实没有该文件，抛 ResourceNotFound；
    其他网络异常抛 RuntimeError。
    """
    safe_path = _validate_relative_path(path)
    last_exc: Exception | None = None
    primary_404_count = 0
    for si, base in enumerate(RESOURCE_BASES):
        url = base + safe_path
        for attempt in range(RETRY_PER_SOURCE):
            try:
                return await asyncio.to_thread(_fetch_url, url, timeout)
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code == 404 and si == 0:
                    primary_404_count += 1
                if e.code in _RETRYABLE_HTTP_CODES and attempt + 1 < RETRY_PER_SOURCE:
                    await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                elif e.code == 404:
                    # 保留末次重试后的退避节奏再切换下个源
                    await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                else:
                    break
            except ValueError as e:
                # 安全校验失败（非白名单/SSRF/响应超限）直接记录并跳过当前源
                last_exc = e
                break
            except Exception as e:  # noqa: BLE001 - 网络异常种类多，逐源回落
                last_exc = e
                if attempt + 1 < RETRY_PER_SOURCE:
                    await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                else:
                    break
    if primary_404_count >= RETRY_PER_SOURCE:
        raise ResourceNotFound(safe_path)
    raise RuntimeError(f"所有资源源拉取 {safe_path} 失败: {last_exc}")


def _load_json_bytes(raw: bytes) -> Any:
    """以 UTF-8（兼容 BOM）解析 JSON 字节串。"""
    return json.loads(raw.decode("utf-8-sig"))


def calc_to_weights(calc: Any) -> tuple[dict[str, int], dict[str, float], list[str]]:
    """把 calc.json 的连续权重（0-2 标度）映射为插件 0-3 档权重。

    分档以该角色最高有效权重为基准做相对映射，与手工模板语义对齐
    （最高档≈核心词条，中档≈重要词条）。
    返回 (0-3 权重, 原始权重, 未识别词条名列表)。
    """
    if not isinstance(calc, dict):
        return {}, {}, []
    sub_props = calc.get("sub_props")
    if not isinstance(sub_props, dict):
        return {}, {}, []

    mapped: dict[str, float] = {}
    unmapped: list[str] = []
    for name, value in sub_props.items():
        if not isinstance(name, str) or isinstance(value, bool):
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v) or v < 0:
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
    if peak <= 0:
        return {}, mapped, unmapped

    weights: dict[str, int] = {}
    for std in STANDARD_SUBSTATS:
        v = mapped.get(std, 0.0)
        if v < BUCKET_LOW * peak:
            weights[std] = 0
        elif v >= BUCKET_HIGH * peak:
            weights[std] = 3
        elif v >= BUCKET_MID * peak:
            weights[std] = 2
        else:
            weights[std] = 1
    return weights, mapped, unmapped


def merge_aliases(
    local: dict[str, list[str]], remote: Any
) -> tuple[dict[str, list[str]], SyncReport]:
    """别名并集合并；本地条目（含自定义昵称）永远保留，同时清洗空白与大小写重复。"""
    report = SyncReport()
    merged: dict[str, list[str]] = {}
    for k, v in local.items():
        if isinstance(k, str) and k.strip() and isinstance(v, list):
            merged[k.strip()] = list(v)

    if not isinstance(remote, dict):
        return merged, report

    for raw_canonical, terms in remote.items():
        if not isinstance(raw_canonical, str) or not isinstance(terms, list):
            continue
        canonical = raw_canonical.strip()
        if not canonical:
            continue

        cleaned_terms: list[str] = []
        seen_remote: set[str] = set()
        for t in terms:
            if not isinstance(t, str):
                continue
            ct = t.strip()
            if not ct:
                continue
            ctl = ct.lower()
            if ctl in seen_remote:
                continue
            seen_remote.add(ctl)
            cleaned_terms.append(ct)

        if not cleaned_terms:
            continue

        if canonical not in merged:
            merged[canonical] = cleaned_terms
            report.alias_canonicals_added.append(canonical)
            continue

        existing_lower = {
            t.strip().lower() for t in merged[canonical] if isinstance(t, str) and t.strip()
        }
        fresh = [t for t in cleaned_terms if t.lower() not in existing_lower]
        if fresh:
            merged[canonical].extend(fresh)
            report.alias_terms_added += len(fresh)
    return merged, report


def validate_weights_data(data: Any) -> list[str]:
    """校验待落盘的 weights.json 数据结构完整性，返回错误列表（空表示合法）。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["weights 根节点必须为 JSON 对象"]

    max_values = data.get("max_values")
    if not isinstance(max_values, dict):
        errors.append("缺少有效的 max_values 对象")
    else:
        for stat in STANDARD_SUBSTATS:
            val = max_values.get(stat)
            if (
                isinstance(val, bool)
                or not isinstance(val, (int, float))
                or not math.isfinite(float(val))
                or float(val) <= 0
            ):
                errors.append(f"max_values 缺少或非法副词条上限: {stat}")

    thresholds = data.get("rank_thresholds")
    if not isinstance(thresholds, dict) or not thresholds:
        errors.append("缺少有效的 rank_thresholds 对象")
    else:
        for rank, th in thresholds.items():
            if (
                not isinstance(rank, str)
                or not rank.strip()
                or isinstance(th, bool)
                or not isinstance(th, (int, float))
                or not math.isfinite(float(th))
            ):
                errors.append(f"rank_thresholds 存在非法阈值: {rank!r}={th!r}")

    characters = data.get("characters")
    if not isinstance(characters, dict) or not characters:
        errors.append("缺少有效的 characters 对象")
    else:
        for cname, cfg in characters.items():
            if not isinstance(cname, str) or not cname.strip() or not isinstance(cfg, dict):
                errors.append(f"角色权重条目非法: {cname!r}")
                continue
            if "_alias_of" in cfg:
                continue
            for stat, weight in cfg.items():
                if stat.startswith("_"):
                    continue
                if (
                    isinstance(weight, bool)
                    or not isinstance(weight, (int, float))
                    or not math.isfinite(float(weight))
                    or float(weight) < 0
                    or float(weight) > 3
                ):
                    errors.append(f"角色 {cname} 词条 {stat} 权重越界: {weight!r}")
    return errors


def validate_aliases_data(data: Any) -> list[str]:
    """校验待落盘的 aliases.json 数据结构完整性，返回错误列表（空表示合法）。"""
    errors: list[str] = []
    if not isinstance(data, dict) or not data:
        return ["aliases 根节点必须为非空 JSON 对象"]
    for canonical, terms in data.items():
        if not isinstance(canonical, str) or not canonical.strip():
            errors.append(f"存在空角色规范名: {canonical!r}")
            continue
        if not isinstance(terms, list) or not terms:
            errors.append(f"角色 {canonical} 别名列表为空或非列表")
            continue
        for term in terms:
            if not isinstance(term, str) or not term.strip():
                errors.append(f"角色 {canonical} 包含空或非字符串别名: {term!r}")
                break
    return errors


def _load_local_json(path: Path, default: Any) -> Any:
    """读取本地 JSON 快照，不存在时返回 default。"""
    if not path.exists():
        return default
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _atomic_write_json(path: Path, data: Any) -> None:
    """安全原子写入 JSON 文件：临时文件落盘 fsync -> 拷贝备份 -> 单步 os.replace。

    绝不先移走原文件，避免并发读取时出现 FileNotFoundError 或中途失败丢失原文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        if path.exists():
            bak = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak)

        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _get_sync_lock(data_dir: Path) -> asyncio.Lock:
    """获取当前事件循环中对应数据目录的并发锁。"""
    loop = asyncio.get_running_loop()
    locks = _LOOP_LOCKS.get(loop)
    if locks is None:
        locks = {}
        _LOOP_LOCKS[loop] = locks
    resolved_dir = data_dir.resolve()
    lock = locks.get(resolved_dir)
    if lock is None:
        lock = asyncio.Lock()
        locks[resolved_dir] = lock
    return lock


async def sync_data(
    data_dir: Path,
    *,
    write: bool = True,
    fetch_existing_calcs: bool = False,
) -> SyncReport:
    """执行一次上游数据同步。write=False 时只计算差异不落盘（--check 模式）。

    fetch_existing_calcs=True 时对非手工维护的已有角色也重新拉取 calc（用于重建自动生成权重）。
    """
    data_dir = Path(data_dir)
    lock = _get_sync_lock(data_dir)
    async with lock:
        return await _sync_data_locked(
            data_dir, write=write, fetch_existing_calcs=fetch_existing_calcs
        )


async def _sync_data_locked(
    data_dir: Path,
    *,
    write: bool,
    fetch_existing_calcs: bool,
) -> SyncReport:
    """在目录并发锁保护下执行同步核心逻辑。"""
    report = SyncReport()

    weights_file = data_dir / "weights.json"
    aliases_file = data_dir / "aliases.json"

    try:
        local_weights = _load_local_json(weights_file, {})
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"本地 weights.json 读取失败: {e}")
        return report

    try:
        local_aliases = _load_local_json(aliases_file, {})
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"本地 aliases.json 读取失败: {e}")
        return report

    if not isinstance(local_weights, dict):
        report.errors.append("本地 weights.json 格式非法(非 JSON 对象)")
        return report
    if not isinstance(local_aliases, dict):
        report.errors.append("本地 aliases.json 格式非法(非 JSON 对象)")
        return report

    characters: dict[str, Any] = dict(local_weights.get("characters") or {})

    # 1. 拉取别名表与 id -> name 映射
    remote_aliases: dict[str, Any] = {}
    try:
        parsed_aliases = _load_json_bytes(await fetch_resource(ALIAS_PATH))
        if isinstance(parsed_aliases, dict):
            remote_aliases = parsed_aliases
        else:
            report.errors.append("别名表格式异常: 上游返回非 JSON 对象")
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"别名表拉取失败: {e}")

    id2name: dict[str, Any] = {}
    try:
        parsed_id2name = _load_json_bytes(await fetch_resource(ID2NAME_PATH))
        if isinstance(parsed_id2name, dict):
            id2name = parsed_id2name
        else:
            report.errors.append("CharId2Data 格式异常: 上游返回非 JSON 对象")
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"CharId2Data 拉取失败: {e}")

    # 2. 筛选需要拉取 calc.json 的角色列表
    existing = set(characters.keys())
    todo_ids: list[str] = []
    for cid, info in id2name.items():
        if not isinstance(cid, str) or not _SAFE_CID_RE.match(cid):
            continue
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        is_manual = (
            name in characters
            and isinstance(characters[name], dict)
            and characters[name].get("_source") == "anyul.cn"
        )
        if name not in existing or (fetch_existing_calcs and not is_manual):
            todo_ids.append(cid)

    # 同名多 id（主角男/女形态等）只取第一个
    seen_names: set[str] = set()
    dedup_ids: list[str] = []
    for cid in todo_ids:
        name = id2name[cid]["name"].strip()
        if name in seen_names:
            continue
        seen_names.add(name)
        dedup_ids.append(cid)

    # 3. 并发拉取 calc.json 并映射权重（并发 4：CNB raw 高并发会触发限流）
    sem = asyncio.Semaphore(4)

    async def _fetch_calc(cid: str) -> tuple[str, dict | None, bool]:
        async with sem:
            try:
                parsed = _load_json_bytes(await fetch_resource(CALC_PATH.format(id=cid)))
                if not isinstance(parsed, dict):
                    report.errors.append(f"{id2name[cid].get('name')} calc 格式异常(非 JSON 对象)")
                    return cid, None, False
                return cid, parsed, False
            except ResourceNotFound:
                # 新角色上游还没生成评分数据，软跳过，下次同步再试
                return cid, None, True
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"{id2name[cid].get('name')} calc 拉取失败: {e}")
                return cid, None, False

    results = await asyncio.gather(*[_fetch_calc(cid) for cid in dedup_ids])
    generated: dict[str, dict[str, Any]] = {}
    for cid, calc, missing in results:
        if missing:
            report.missing_calc.append(str(id2name[cid].get("name", cid)).strip())
            continue
        if not calc:
            continue
        name = id2name[cid]["name"].strip()
        weights, raw, unmapped = calc_to_weights(calc)
        if unmapped:
            report.unmapped_substats[name] = unmapped
        if not weights:
            report.errors.append(f"{name} calc 无有效权重, 跳过")
            continue
        desc_bits = ", ".join(
            f"{k}{v:g}" for k, v in sorted(raw.items(), key=lambda kv: -kv[1])[:4]
        )
        generated[name] = {
            "_desc": f"自动生成 (XutheringWavesUID calc: {calc.get('name', name)}; 主要词条 {desc_bits})",
            **weights,
        }

    if fetch_existing_calcs:
        changed_chars = {k: v for k, v in generated.items() if characters.get(k) != v}
    else:
        changed_chars = {k: v for k, v in generated.items() if k not in existing}

    if changed_chars:
        report.new_characters = sorted(k for k in changed_chars if k not in existing)
        characters.update(changed_chars)

    # 4. 别名并集合并
    merged_aliases, alias_report = merge_aliases(local_aliases, remote_aliases)
    report.alias_canonicals_added = alias_report.alias_canonicals_added
    report.alias_terms_added = alias_report.alias_terms_added

    # 5. Schema 校验通过后原子落盘（绝不用坏数据覆盖本地完好快照）
    if write and changed_chars:
        candidate_weights = dict(local_weights)
        candidate_weights["characters"] = characters
        validation_errors = validate_weights_data(candidate_weights)
        if validation_errors:
            report.errors.append(
                f"weights.json 校验失败，已放弃写入以保护本地数据: {validation_errors[0]}"
            )
        else:
            _atomic_write_json(weights_file, candidate_weights)
            report.weights_changed = True

    if write and (report.alias_canonicals_added or report.alias_terms_added):
        alias_errors = validate_aliases_data(merged_aliases)
        if alias_errors:
            report.errors.append(
                f"aliases.json 校验失败，已放弃写入以保护本地数据: {alias_errors[0]}"
            )
        else:
            _atomic_write_json(aliases_file, merged_aliases)
            report.aliases_changed = True

    return report


def main(argv: list[str]) -> int:
    """命令行独立运行入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="鸣潮声骸插件数据同步")
    parser.add_argument("--data-dir", default=str(Path(__file__).parent.parent / "data"))
    parser.add_argument("--check", action="store_true", help="只检查不写入")
    args = parser.parse_args(argv)

    async def _run() -> int:
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
