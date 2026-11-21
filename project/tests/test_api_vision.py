"""素材接口（`/api/images`、`/api/vision/inspect`）与诊断接入的测试。

素材层有三条容易坏的地方，这里各钉一条：
1. 图片走的是库旁边的 `data/images/`（相对 `DB_PATH` 推导），路径算错就 404；
2. `mode=mock` 必须在**不发视觉请求**的前提下也能返回属性（离线演示的前提）；
3. 诊断卡只有在 `with_material=True` 且卡是 SKU 级时才挂素材块（与根因码无关）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.main import create_app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


def _call(method: str, url: str, **kwargs):
    async def run():
        async with _client() as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


def _first_sku(db_path: Path) -> str:
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT sku_id FROM image_feature ORDER BY sku_id LIMIT 1").fetchone()
    finally:
        con.close()
    assert row is not None, "测试库没有 image_feature 行"
    return row[0]


def test_images_dir_follows_db_path(db_path: Path) -> None:
    """图片目录 = 库文件同级的 images/，与配置里的 DB_PATH 一起走。"""
    from app.api.routes import _images_dir

    assert _images_dir() == db_path.resolve().parent / "images"


def test_image_missing_404(db_path: Path) -> None:
    sku = _first_sku(db_path)
    resp = _call("GET", f"/api/images/{sku}?role=main")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "image_missing"


def test_image_ok(db_path: Path) -> None:
    sku = _first_sku(db_path)
    img_dir = db_path.resolve().parent / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    target = img_dir / f"{sku}_main.png"
    Image.new("RGB", (32, 32), (240, 236, 230)).save(target)
    try:
        resp = _call("GET", f"/api/images/{sku}?role=main")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        target.unlink(missing_ok=True)


def test_image_bad_role_400(db_path: Path) -> None:
    sku = _first_sku(db_path)
    resp = _call("GET", f"/api/images/{sku}?role=cover")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_role"


def test_vision_inspect_mock(db_path: Path) -> None:
    """mock 只读 image_feature 真值，不碰模型：离线也能出属性、问题码与证据。"""
    sku = _first_sku(db_path)
    resp = _call("POST", "/api/vision/inspect", json={"sku_id": sku, "role": "main", "mode": "mock"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sku_id"] == sku and body["role"] == "main"
    assert body["features"]["source"] == "mock"
    assert isinstance(body["features"]["quality_flags"], list)
    assert body["evidence"]["kind"] == "material"
    assert body["image_url"].endswith(f"/api/images/{sku}?role=main")
    assert body["meta"]["mode"] == "mock" and body["meta"]["elapsed_ms"] >= 0
    # 问题码必须是 schema 认识的码，不能把中文标签直接吐给前端
    for issue in body["issues"]:
        assert issue["code"].isupper()


def test_vision_inspect_bad_mode_400(db_path: Path) -> None:
    sku = _first_sku(db_path)
    resp = _call("POST", "/api/vision/inspect", json={"sku_id": sku, "mode": "screenshot"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_mode"


def test_vision_inspect_unknown_sku_404(db_path: Path) -> None:
    resp = _call("POST", "/api/vision/inspect", json={"sku_id": "SKU9999", "mode": "mock"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "sku_not_found"


def test_diagnose_material_flag(db_path: Path) -> None:
    """`material=true` 时 meta 里必须有素材汇总；不开时明确 requested=False。"""
    off = _call("GET", "/api/diagnose?as_of=2026-11-18&material=false")
    assert off.status_code == 200
    assert off.json()["meta"]["material"]["requested"] is False

    on = _call("GET", "/api/diagnose?as_of=2026-11-18&material=true")
    assert on.status_code == 200
    material = on.json()["meta"]["material"]
    assert material["requested"] is True
    assert material["attached"] <= material["targets"]
    # 挂上的卡要带素材块，且块里必须有 evidence 文本
    for card in on.json()["cards"]:
        if card.get("material"):
            assert card["material"]["evidence"]["kind"] == "material"
            assert card["material"]["evidence"]["text"]


def test_material_attach_helper_skips_non_sku_cards(db_path: Path) -> None:
    """素材只挂 SKU 级卡：其它维度的卡不该被硬塞「主图里看到什么」。"""
    from app.diagnose.diagnosis import _attach_material

    cards = [
        {"code": "R7", "scope": {"dim": "style", "dim_value": "甜美"}, "caveats": []},
        {"code": "R8", "scope": {"dim": "channel_id", "dim_value": "CH_SEARCH"}, "caveats": []},
    ]
    meta = _attach_material(db_path, cards)
    assert meta["attached"] == 0
    assert all("material" not in c for c in cards)


def test_material_attach_any_sku_card(db_path: Path) -> None:
    """非 R2/R4 的 SKU 卡也要挂素材：演示日（11-17）出的是 R3，入口不能因此消失。"""
    from app.diagnose.diagnosis import _attach_material

    cards = [
        {"code": "R3", "scope": {"dim": "sku_id", "dim_value": _first_sku(db_path)}, "caveats": []}
    ]
    meta = _attach_material(db_path, cards)
    assert meta["targets"] == 1
    assert meta["attached"] == 1
    block = cards[0]["material"]
    assert block["evidence"]["kind"] == "material"
    assert block["evidence"]["text"]
    assert 0.0 <= block["score"] <= 1.0
    assert isinstance(block["findings"], list)
    assert "degraded" in block


@pytest.mark.parametrize("flag", ["NO_SIZE_CHART", "TEXT_HEAVY"])
def test_issues_map_back_to_root_causes(flag: str) -> None:
    """素材问题必须能折回根因码，否则「素材 → 策略卡」这条链就断了。"""
    from app.vision.schema import ISSUE_SPECS

    spec = ISSUE_SPECS[flag]
    assert spec["root_causes"], f"{flag} 没有关联根因"
    assert all(code.startswith("R") for code in spec["root_causes"])
    assert spec["action"] == "swap_creative"
