"""上游数据同步（core/sync.py）全离线 Mock、SSRF 防护与原子写盘测试。"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core import sync


def _base_weights() -> dict:
    return {
        "max_values": {
            "暴击": 10.5,
            "暴击伤害": 21.0,
            "攻击%": 11.6,
            "生命%": 11.6,
            "防御%": 14.7,
            "共鸣效率": 11.6,
            "普攻伤害加成": 11.6,
            "重击伤害加成": 11.6,
            "共鸣技能伤害加成": 11.6,
            "共鸣解放伤害加成": 11.6,
            "攻击": 60.0,
            "生命": 580.0,
            "防御": 70.0,
        },
        "characters": {
            "generic_crit": dict.fromkeys(sync.STANDARD_SUBSTATS, 1),
            "今汐": {
                "_desc": "今汐 (anyul.cn)",
                "_source": "anyul.cn",
                **dict.fromkeys(sync.STANDARD_SUBSTATS, 2),
            },
        },
        "rank_thresholds": {"ACE": 45, "SSS": 35, "SS": 25, "S": 18, "A": 10, "N": 0},
    }


def test_calc_to_weights_and_merge_aliases():
    """验证连续权重相对分档、异常值过滤与别名清洗合并。"""
    weights, raw, unmapped = sync.calc_to_weights(
        {
            "sub_props": {
                "暴击": 2.0,  # 100% -> 3
                "暴击伤害": 1.0,  # 50% -> 2
                "技能伤害加成": 0.5,  # 映射为共鸣技能伤害加成 25% -> 1
                "攻击": 0.1,  # 5% -> 0
                "防御": float("nan"),  # 过滤
                "生命": -1.0,  # 过滤
                "共鸣效率": True,  # 过滤 bool
                "神秘新属性": 0.8,  # 记录未识别
            }
        }
    )
    assert weights["暴击"] == 3
    assert weights["暴击伤害"] == 2
    assert weights["共鸣技能伤害加成"] == 1
    assert weights["攻击"] == 0
    assert unmapped == ["神秘新属性"]
    assert raw["暴击"] == 2.0

    # 别名合并：清洗空白、去重、绝不覆盖本地自定义昵称
    merged, report = sync.merge_aliases(
        {"今汐": ["今汐", "本地自定义"]},
        {
            "今汐": ["今汐", " 汐汐 ", "JX", "jx", ""],
            "新角色": ["新角色", " new_char ", "NEW_CHAR"],
        },
    )
    assert merged["今汐"] == ["今汐", "本地自定义", "汐汐", "JX"]
    assert merged["新角色"] == ["新角色", "new_char"]
    assert report.alias_canonicals_added == ["新角色"]
    assert report.alias_terms_added == 2


def test_ssrf_url_and_path_validation():
    """验证 URL 白名单、HTTPS 强制、内网 IP 拒绝、重定向校验与路径穿越拦截。"""
    # 合法白名单 URL 放行
    assert sync._validate_url(sync.RESOURCE_BASES[0] + sync.ALIAS_PATH).startswith("https://")

    # 各类危险 URL 必须拒绝
    for bad_url in [
        "",
        "http://cnb.cool/a.json",
        "file:///etc/passwd",
        "ftp://cnb.cool/a.json",
        "https://user:pass@cnb.cool/a.json",
        "https://127.0.0.1/secret",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/secret",
        "https://evil.example.com/a.json",
        "https://cnb.cool/a\r\nX-Inject: 1",
    ]:
        with pytest.raises(ValueError):
            sync._validate_url(bad_url)

    # 危险相对路径拦截
    for bad_path in ["", "/etc/passwd", "../secret.json", "map/../../etc/passwd", "a\\b.json"]:
        with pytest.raises(ValueError):
            sync._validate_relative_path(bad_path)

    # 302 重定向到内网/非白名单域名必须在 RedirectHandler 阶段拦截
    handler = sync._SafeRedirectHandler()
    req = urllib.request.Request(sync.RESOURCE_BASES[0] + sync.ALIAS_PATH)
    with pytest.raises(ValueError, match="不允许直接访问 IP"):
        handler.redirect_request(req, None, 302, "Found", {}, "https://127.0.0.1/admin")
    with pytest.raises(ValueError, match="仅允许 HTTPS"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://cnb.cool/downgrade")


def test_fetch_url_response_size_limit(monkeypatch: pytest.MonkeyPatch):
    """验证单次远程响应超过 MAX_RESPONSE_BYTES 时主动拒绝防止 OOM。"""

    class _HugeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return sync.RESOURCE_BASES[0] + sync.ALIAS_PATH

        def read(self, n: int = -1):
            return b"x" * n

    class _FakeOpener:
        def open(self, req, timeout=None):
            return _HugeResp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: _FakeOpener())
    monkeypatch.setattr(sync, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(ValueError, match="超过大小上限"):
        sync._fetch_url(sync.RESOURCE_BASES[0] + sync.ALIAS_PATH)


def test_fetch_resource_retry_fallback_and_404(monkeypatch: pytest.MonkeyPatch):
    """验证主源 404 重试、备用源回落以及全源 404 抛 ResourceNotFound。"""
    monkeypatch.setattr(sync, "RETRY_BACKOFF", 0.001)

    # 场景 1: 主源第 1 次 404，第 2 次成功
    attempts = {"n": 0}

    def _flaky_fetch(url: str, timeout: float = 30.0) -> bytes:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
        return b'{"ok": 1}'

    monkeypatch.setattr(sync, "_fetch_url", _flaky_fetch)
    assert asyncio.run(sync.fetch_resource(sync.ALIAS_PATH)) == b'{"ok": 1}'
    assert attempts["n"] == 2

    # 场景 2: 主源网络异常，自动回落到第二个源成功
    def _fallback_fetch(url: str, timeout: float = 30.0) -> bytes:
        if "cnb.cool" in url:
            raise OSError("connection reset")
        return b'{"from": "backup"}'

    monkeypatch.setattr(sync, "_fetch_url", _fallback_fetch)
    assert asyncio.run(sync.fetch_resource(sync.ALIAS_PATH)) == b'{"from": "backup"}'

    # 场景 3: 所有源均 404 -> 抛出 ResourceNotFound
    def _always_404(url: str, timeout: float = 30.0) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(sync, "_fetch_url", _always_404)
    with pytest.raises(sync.ResourceNotFound):
        asyncio.run(sync.fetch_resource("map/character/9999/calc.json"))


def test_sync_data_success_check_mode_and_manual_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """验证完整离线同步、手工条目保护、路径穿越 cid 过滤、--check 模式与 .bak 备份。"""
    weights_file = tmp_path / "weights.json"
    aliases_file = tmp_path / "aliases.json"
    weights_file.write_text(json.dumps(_base_weights(), ensure_ascii=False), encoding="utf-8")
    aliases_file.write_text(
        json.dumps({"今汐": ["今汐", "龙女"]}, ensure_ascii=False), encoding="utf-8"
    )

    remote_map = {
        sync.ALIAS_PATH: json.dumps(
            {"今汐": ["今汐", " 汐汐 "], "新角色A": ["新角色A", "newA"]}, ensure_ascii=False
        ).encode("utf-8"),
        sync.ID2NAME_PATH: json.dumps(
            {
                "1001": {"name": "今汐"},
                "2001": {"name": "新角色A"},
                "2002": {"name": "新角色A"},  # 同名多 ID 去重
                "3001": {"name": "暂无数据角色"},
                "../evil": {"name": "恶意注入"},  # 非法 cid 必须忽略
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        "map/character/2001/calc.json": json.dumps(
            {"name": "新角色A-通用", "sub_props": {"暴击": 2.0, "攻击%": 1.2}},
            ensure_ascii=False,
        ).encode("utf-8"),
    }

    async def _fake_fetch_resource(path: str, timeout: float = 30.0) -> bytes:
        if path == "map/character/3001/calc.json":
            raise sync.ResourceNotFound(path)
        if path in remote_map:
            return remote_map[path]
        raise AssertionError(f"意外请求的资源路径: {path}")

    monkeypatch.setattr(sync, "fetch_resource", _fake_fetch_resource)

    # 先测 --check 模式（write=False）：算差异但不落盘
    check_report = asyncio.run(sync.sync_data(tmp_path, write=False))
    assert check_report.new_characters == ["新角色A"]
    assert check_report.missing_calc == ["暂无数据角色"]
    assert not check_report.weights_changed
    assert not check_report.aliases_changed
    assert "新角色A" not in json.loads(weights_file.read_text(encoding="utf-8"))["characters"]

    # 正式写入模式（write=True）
    write_report = asyncio.run(sync.sync_data(tmp_path, write=True))
    assert write_report.weights_changed is True
    assert write_report.aliases_changed is True
    assert write_report.new_characters == ["新角色A"]
    assert write_report.errors == []

    # 校验备份生成、手工条目未被覆盖、新角色已入库且文件末尾有换行
    assert (tmp_path / "weights.json.bak").exists()
    assert (tmp_path / "aliases.json.bak").exists()
    raw_weights_text = weights_file.read_text(encoding="utf-8")
    assert raw_weights_text.endswith("\n")
    saved_weights = json.loads(raw_weights_text)
    assert saved_weights["characters"]["今汐"]["_source"] == "anyul.cn"
    assert saved_weights["characters"]["新角色A"]["暴击"] == 3
    assert "恶意注入" not in saved_weights["characters"]

    saved_aliases = json.loads(aliases_file.read_text(encoding="utf-8"))
    assert saved_aliases["今汐"] == ["今汐", "龙女", "汐汐"]
    assert saved_aliases["新角色A"] == ["新角色A", "newA"]


def test_sync_data_bad_schema_rollback_and_atomic_write_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """验证 Schema 校验拒绝坏数据落盘，以及原子写盘中途失败不破坏原文件。"""
    weights_file = tmp_path / "weights.json"
    aliases_file = tmp_path / "aliases.json"

    # 本地 weights.json 缺少 max_values（损坏结构），同步绝不能拿残缺数据覆盖它
    broken_local = {"characters": {"generic_crit": {"暴击": 3}}}
    original_text = json.dumps(broken_local, ensure_ascii=False)
    weights_file.write_text(original_text, encoding="utf-8")
    aliases_file.write_text(json.dumps({"今汐": ["今汐"]}, ensure_ascii=False), encoding="utf-8")

    async def _fake_fetch(path: str, timeout: float = 30.0) -> bytes:
        if path == sync.ALIAS_PATH:
            return b'["not-a-dict"]'  # 上游返回非法 JSON 结构
        if path == sync.ID2NAME_PATH:
            return json.dumps({"2001": {"name": "新角色B"}}).encode("utf-8")
        return json.dumps({"sub_props": {"暴击": 2.0}}).encode("utf-8")

    monkeypatch.setattr(sync, "fetch_resource", _fake_fetch)

    report = asyncio.run(sync.sync_data(tmp_path, write=True))
    assert not report.weights_changed
    assert not report.aliases_changed
    assert any("校验失败" in err for err in report.errors)
    assert any("格式异常" in err for err in report.errors)
    # 原文件毫发无损
    assert weights_file.read_text(encoding="utf-8") == original_text

    # 测试 _atomic_write_json 序列化失败时不破坏已有文件且不残留 .tmp 文件
    good_file = tmp_path / "good.json"
    good_file.write_text('{"safe": true}\n', encoding="utf-8")
    with pytest.raises(TypeError):
        sync._atomic_write_json(good_file, {"bad": object()})
    assert good_file.read_text(encoding="utf-8") == '{"safe": true}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_sync_data_concurrent_lock_and_cli_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """验证并发同步由目录锁串行保护，以及独立 CLI --check 模式可正常运行。"""
    weights_file = tmp_path / "weights.json"
    aliases_file = tmp_path / "aliases.json"
    weights_file.write_text(json.dumps(_base_weights(), ensure_ascii=False), encoding="utf-8")
    aliases_file.write_text(json.dumps({"今汐": ["今汐"]}, ensure_ascii=False), encoding="utf-8")

    in_flight = 0
    max_parallel_syncs = 0

    async def _slow_fetch(path: str, timeout: float = 30.0) -> bytes:
        nonlocal in_flight, max_parallel_syncs
        in_flight += 1
        max_parallel_syncs = max(max_parallel_syncs, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if path == sync.ALIAS_PATH:
            return json.dumps({"今汐": ["今汐"]}, ensure_ascii=False).encode("utf-8")
        return b"{}"

    monkeypatch.setattr(sync, "fetch_resource", _slow_fetch)

    async def _run_concurrent():
        return await asyncio.gather(
            sync.sync_data(tmp_path, write=True),
            sync.sync_data(tmp_path, write=True),
        )

    r1, r2 = asyncio.run(_run_concurrent())
    assert r1.errors == [] and r2.errors == []
    # 因目录锁串行执行，两个 sync_data 的 fetch 不会交错并发
    assert max_parallel_syncs == 1

    # CLI --check 模式返回 0
    exit_code = sync.main(["--data-dir", str(tmp_path), "--check"])
    assert exit_code == 0


def test_sync_data_fetch_existing_calcs_updates_auto_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """验证 fetch_existing_calcs=True 会刷新自动生成角色但绝不覆盖手工 anyul.cn 条目。"""
    weights = _base_weights()
    weights["characters"]["自动角色"] = {
        "_desc": "自动生成 (旧快照)",
        **dict.fromkeys(sync.STANDARD_SUBSTATS, 0),
    }
    weights_file = tmp_path / "weights.json"
    aliases_file = tmp_path / "aliases.json"
    weights_file.write_text(json.dumps(weights, ensure_ascii=False), encoding="utf-8")
    aliases_file.write_text(json.dumps({"今汐": ["今汐"]}, ensure_ascii=False), encoding="utf-8")

    async def _fake_fetch(path: str, timeout: float = 30.0) -> bytes:
        if path == sync.ALIAS_PATH:
            return json.dumps({"今汐": ["今汐"]}, ensure_ascii=False).encode("utf-8")
        if path == sync.ID2NAME_PATH:
            return json.dumps(
                {"1001": {"name": "今汐"}, "2001": {"name": "自动角色"}}, ensure_ascii=False
            ).encode("utf-8")
        if path == "map/character/2001/calc.json":
            return json.dumps({"sub_props": {"暴击": 2.0}}, ensure_ascii=False).encode("utf-8")
        raise AssertionError(f"手工条目不应被重新拉取: {path}")

    monkeypatch.setattr(sync, "fetch_resource", _fake_fetch)
    report = asyncio.run(sync.sync_data(tmp_path, write=True, fetch_existing_calcs=True))
    assert report.weights_changed is True
    saved = json.loads(weights_file.read_text(encoding="utf-8"))
    assert saved["characters"]["自动角色"]["暴击"] == 3
    assert saved["characters"]["今汐"]["_source"] == "anyul.cn"
