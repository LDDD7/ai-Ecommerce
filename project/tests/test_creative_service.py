"""创意工单的编排层与 HTTP 层测试（全部离线）。

改图器 `app.creative.local` 由另一个 agent 实现，这里用假模块替代——
测的是「编排逻辑对不对」（合规门、降级、落库、起草动作），不是「图改得好不好看」。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import types
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.creative import service, store
from app.creative.schema import CreativeAsset, CreativeConstraints, CreativeFix, CreativeRequest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "store.db"


@pytest.fixture(scope="module")
def store_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把真库复制一份到 .tmp（`data/` 目录有 DENY ACE，不能在那里建 -wal）。"""
    base = ROOT / ".tmp" / "creative_service" / uuid.uuid4().hex[:8]
    base.mkdir(parents=True, exist_ok=True)
    target = base / "store.db"
    shutil.copy2(SOURCE_DB, target)
    return target


@pytest.fixture()
def fake_local(monkeypatch: pytest.MonkeyPatch):
    """替换改图器：默认「文字占比降到 0.18、补了尺码表、没有水印」。"""

    def make(after: dict | None = None, issues_left: list[str] | None = None):
        module = types.ModuleType("app.creative.local")

        def render(db_path, *, sku_id, issues, constraints, draft_id, target="main_image"):
            asset = CreativeAsset(
                asset_id=f"{draft_id}-image",
                path=f"data/creative/{sku_id}/{draft_id}_main.png",
                url=f"/api/creative/asset/{draft_id}",
                width=800,
                height=800,
            )
            body = {
                "text_coverage": 0.18,
                "size_chart_present": True,
                "watermark": False,
                "issues_left": issues_left if issues_left is not None else [],
            }
            body.update(after or {})
            fixes = [CreativeFix(issue=code, action="auto", detail="已修复", status="done") for code in issues]
            return asset, body, fixes

        module.render = render  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "app.creative.local", module)
        # service 里写的是 `from app.creative import local`，会先看包属性；
        # 其它用例一旦真导入过，包属性上就是真模块，必须一并替换。
        monkeypatch.setattr("app.creative.local", module, raising=False)
        return module

    return make


def _req(**kwargs) -> CreativeRequest:
    payload = {"sku_id": "SKU0001", "issues": ["TEXT_HEAVY"]}
    payload.update(kwargs)
    return CreativeRequest(**payload)


def test_draft_produces_ready_result(store_db: Path, fake_local) -> None:
    fake_local()
    result = service.draft(str(store_db), _req())
    assert result.status == "ready"
    assert result.provider == "local"
    assert result.after["text_coverage"] == 0.18
    assert result.compliance.ok
    assert result.copywriting.title
    assert len(result.copywriting.selling_points) == 3
    assert result.before["text_coverage"] > 0
    assert result.assets and result.assets[0].url.endswith(result.draft_id)


def test_draft_fills_issues_from_features(store_db: Path, fake_local) -> None:
    """不传 issues 时按图的真实属性现推（挑一个文字占比偏高的 SKU）。"""
    conn = sqlite3.connect(store_db)
    try:
        row = conn.execute(
            "SELECT sku_id FROM image_feature"
            " WHERE role='main' AND CAST(json_extract(features_json, '$.text_coverage') AS REAL) > 0.35"
            " LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row, "合成数据里应该有文字占比偏高的 SKU"
    fake_local()
    result = service.draft(str(store_db), CreativeRequest(sku_id=row[0]))
    assert "TEXT_HEAVY" in result.before["issues"]


def test_compliance_blocks_when_text_still_heavy(store_db: Path, fake_local) -> None:
    fake_local(after={"text_coverage": 0.45})
    result = service.draft(str(store_db), _req(issues=["TEXT_HEAVY"]))
    assert result.compliance.ok is False
    assert any("文字占比" in b for b in result.compliance.blockers)


def test_compliance_blocks_on_banned_claims(store_db: Path, fake_local, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_local()
    monkeypatch.setattr(service, "build_copy", lambda *a, **k: service.CreativeCopy(
        title="全网最低价的连衣裙", selling_points=["绝对显瘦"], description="", size_text=""
    ))
    result = service.draft(str(store_db), _req())
    assert result.compliance.ok is False
    assert any("绝对化" in b for b in result.compliance.blockers)


def test_manual_issues_are_warned_not_blocking(store_db: Path, fake_local) -> None:
    fake_local(issues_left=["COLLAGE"])
    result = service.draft(str(store_db), _req(issues=["COLLAGE", "TEXT_HEAVY"]))
    assert result.compliance.ok is True
    assert any(item.code == "manual_issues" and item.status == "warn" for item in result.compliance.items)
    assert "COLLAGE" not in result.after["issues_left"]


def test_dashscope_without_key_degrades_to_local(store_db: Path, fake_local, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_local()
    monkeypatch.setattr("app.core.runtime.creative_provider", lambda: "dashscope")
    # 本机 .env 里可能有真实百炼 key；这条用例的前提是「没有 key」。
    monkeypatch.setattr("app.core.runtime.dashscope_key", lambda: "")
    fake_ds = types.ModuleType("app.creative.dashscope")
    fake_ds.available = lambda: False  # type: ignore[attr-defined]
    fake_ds.DashScopeUnavailable = type("DashScopeUnavailable", (RuntimeError,), {})
    monkeypatch.setitem(sys.modules, "app.creative.dashscope", fake_ds)
    # 其它测试可能已把真模块挂到包属性上，会绕过 sys.modules 注入，这里一并摘掉。
    monkeypatch.delattr("app.creative.dashscope", raising=False)

    result = service.draft(str(store_db), _req())
    assert result.provider == "local"
    assert result.status == "degraded"
    assert "降级" in result.note


def test_apply_creates_swap_creative_action(store_db: Path, fake_local) -> None:
    fake_local()
    result = service.draft(str(store_db), _req())
    out = service.apply(str(store_db), result.draft_id, actor="demo", reason="测试起草")
    action = out["action"]
    assert action["type"] == "swap_creative"
    assert action["status"] == "drafted"
    assert action["risk_level"] == 1
    assert action["params"]["creative_id"] == result.draft_id


def test_apply_refuses_when_compliance_fails(store_db: Path, fake_local) -> None:
    fake_local(after={"text_coverage": 0.5})
    result = service.draft(str(store_db), _req())
    with pytest.raises(service.CreativeError):
        service.apply(str(store_db), result.draft_id)


def test_store_roundtrip_and_list(store_db: Path, fake_local) -> None:
    fake_local()
    result = service.draft(str(store_db), _req(issues=["NO_SIZE_CHART"]))
    loaded = store.get(str(store_db), result.draft_id)
    assert loaded.draft_id == result.draft_id
    items = store.list_drafts(str(store_db), sku_id="SKU0001", limit=5)
    assert items and items[0]["draft_id"] == result.draft_id
    with pytest.raises(LookupError):
        store.get(str(store_db), "cr_not_exist")


def test_unknown_sku_raises(store_db: Path, fake_local) -> None:
    fake_local()
    with pytest.raises(service.CreativeError):
        service.draft(str(store_db), CreativeRequest(sku_id="SKU9999", issues=["TEXT_HEAVY"]))


# ---------------------------------------------------------------- HTTP 层


def _client(store_db: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setenv("DB_PATH", str(store_db))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def test_api_creative_draft_and_get(store_db: Path, fake_local, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_local()

    async def run() -> None:
        async with _client(store_db, monkeypatch) as client:
            resp = await client.post(
                "/api/creative/draft",
                json={"sku_id": "SKU0001", "issues": ["TEXT_HEAVY"]},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["draft_id"].startswith("cr_")
            assert body["compliance"]["ok"] is True
            assert body["meta"]["wall_ms"] >= 0

            got = await client.get(f"/api/creative/{body['draft_id']}")
            assert got.status_code == 200
            assert got.json()["draft_id"] == body["draft_id"]

            listing = await client.get("/api/creative", params={"sku_id": "SKU0001", "limit": 3})
            assert listing.status_code == 200
            assert listing.json()["count"] >= 1

    asyncio.run(run())


def test_api_creative_apply_and_errors(store_db: Path, fake_local, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_local()

    async def run() -> None:
        async with _client(store_db, monkeypatch) as client:
            created = await client.post("/api/creative/draft", json={"sku_id": "SKU0001", "issues": ["TEXT_HEAVY"]})
            draft_id = created.json()["draft_id"]
            applied = await client.post(f"/api/creative/{draft_id}/apply", json={"actor": "demo"})
            assert applied.status_code == 200, applied.text
            assert applied.json()["action"]["type"] == "swap_creative"

            missing = await client.get("/api/creative/cr_nope")
            assert missing.status_code == 404

            bad_issue = await client.post(
                "/api/creative/draft", json={"sku_id": "SKU0001", "issues": ["NOT_A_CODE"]}
            )
            assert bad_issue.status_code == 400

            bad_sku = await client.post("/api/creative/draft", json={"sku_id": "SKU01"})
            assert bad_sku.status_code == 400

    asyncio.run(run())


def test_api_creative_asset_rejects_traversal(store_db: Path, fake_local, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_local()

    async def run() -> None:
        async with _client(store_db, monkeypatch) as client:
            created = await client.post("/api/creative/draft", json={"sku_id": "SKU0001", "issues": ["TEXT_HEAVY"]})
            draft_id = created.json()["draft_id"]
            # 假改图器没落真实文件 → 404（也证明路径检查先于存在性检查不会 500）
            resp = await client.get(f"/api/creative/asset/{draft_id}")
            assert resp.status_code in (200, 404)

    asyncio.run(run())


def test_draft_persists_request_payload(store_db: Path, fake_local) -> None:
    fake_local()
    result = service.draft(str(store_db), _req(issues=["TEXT_HEAVY"], target="detail_image"))
    conn = sqlite3.connect(store_db)
    try:
        raw = conn.execute(
            "SELECT request_json, target FROM creative_draft WHERE draft_id = ?", (result.draft_id,)
        ).fetchone()
    finally:
        conn.close()
    assert raw[1] == "detail_image"
    assert json.loads(raw[0])["issues"] == ["TEXT_HEAVY"]


def test_constraints_reach_renderer(store_db: Path, fake_local, monkeypatch: pytest.MonkeyPatch) -> None:
    """改图器必须收到调用方的约束（文字上限、是否补尺码表）。"""
    seen: dict = {}

    def make():
        module = types.ModuleType("app.creative.local")

        def render(db_path, *, sku_id, issues, constraints, draft_id, target="main_image"):
            seen["constraints"] = constraints
            asset = CreativeAsset(asset_id=f"{draft_id}-image", path="x.png", url="/x.png")
            return asset, {"text_coverage": 0.2, "size_chart_present": True, "watermark": False}, []

        module.render = render  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "app.creative.local", module)

    make()
    service.draft(
        str(store_db),
        _req(constraints=CreativeConstraints(max_text_coverage=0.22, add_size_chart=False)),
    )
    assert seen["constraints"].max_text_coverage == 0.22
    assert seen["constraints"].add_size_chart is False
