"""促销日历（spec §28）的存取、回写与接口测试。

三件事最容易悄悄坏掉，各钉一条：

1. 日历改了之后 `dim_date.is_promo` 没跟上（诊断看到的是旧标记）；
2. 日历空的时候把生成器写好的演示标记抹平（保守跳过必须保留）；
3. 导入真实数据后没有自动重算（商家导完双 11 订单，R7 却永远不出现）。
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest

from app import promo
from app.ingest import store
from app.main import create_app


@pytest.fixture()
def scratch(db_path: Path) -> Path:
    """每个用例一份独立的库副本，互不影响（库是 session 级共享的）。"""
    target = Path(".tmp") / "promo_tests" / f"{db_path.stem}_{uuid.uuid4().hex[:8]}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, target)
    promo.ensure_schema(target)
    yield target
    try:
        target.unlink()
    except PermissionError:
        pass


def _flags(db: Path, start: str, end: str) -> list[tuple[str, int, str | None]]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT dt, is_promo, promo_name FROM dim_date WHERE dt BETWEEN ? AND ? ORDER BY dt",
            (start, end),
        ).fetchall()
    finally:
        conn.close()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


def _call(method: str, url: str, **kwargs):
    async def run():
        async with _client() as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


def test_create_and_list(scratch: Path) -> None:
    promo.create_period(scratch, name="店庆", start_dt="2026-11-20", end_dt="2026-11-22", kind="store")
    periods = promo.list_periods(scratch)
    assert [p["name"] for p in periods] == ["店庆"]
    assert periods[0]["days"] == 3
    assert periods[0]["kind_label"] == "店铺活动"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(name="  ", start_dt="2026-11-20", end_dt="2026-11-21"), "名称不能为空"),
        (dict(name="倒置", start_dt="2026-11-22", end_dt="2026-11-20"), "结束日期不能早于"),
        (dict(name="太长", start_dt="2026-01-01", end_dt="2026-06-01"), "最多 90 天"),
        (dict(name="坏日期", start_dt="2026/11/20", end_dt="2026-11-21"), "格式应为"),
        (dict(name="坏类型", start_dt="2026-11-20", end_dt="2026-11-21", kind="bigsale"), "类型只能是"),
    ],
)
def test_validation(scratch: Path, kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError) as err:
        promo.create_period(scratch, **kwargs)
    assert message in str(err.value)


def test_rebuild_marks_and_clears(scratch: Path) -> None:
    period = promo.create_period(scratch, name="黑五", start_dt="2026-11-20", end_dt="2026-11-22")
    sync = promo.rebuild_flags(scratch, start="2026-11-19", end="2026-11-23", force=True)
    assert sync["rebuilt"] and sync["promo_days"] == 3
    rows = _flags(scratch, "2026-11-19", "2026-11-23")
    assert [r[1] for r in rows] == [0, 1, 1, 1, 0]
    assert rows[1][2] == "黑五"

    # 改期：旧日期必须被清掉，否则等于「促销越改越多」
    promo.update_period(scratch, period["promo_id"], start_dt="2026-11-22", end_dt="2026-11-23")
    promo.rebuild_flags(scratch, start="2026-11-19", end="2026-11-24", force=True)
    rows = _flags(scratch, "2026-11-20", "2026-11-24")
    assert [r[1] for r in rows] == [0, 0, 1, 1, 0]


def test_empty_calendar_keeps_seeded_flags(scratch: Path) -> None:
    """演示库的 50 个促销日是生成器写的，日历为空时不能被抹平。"""
    conn = sqlite3.connect(str(scratch))
    conn.execute("DELETE FROM promo_calendar")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM dim_date WHERE is_promo = 1").fetchone()[0]
    conn.close()

    sync = promo.rebuild_flags(scratch, start="2026-10-20", end="2026-11-11")
    assert sync["rebuilt"] is False
    assert "为空" in sync["reason"]

    conn = sqlite3.connect(str(scratch))
    after = conn.execute("SELECT COUNT(*) FROM dim_date WHERE is_promo = 1").fetchone()[0]
    conn.close()
    assert after == before


def test_delete_last_period_clears_flags(scratch: Path) -> None:
    conn = sqlite3.connect(str(scratch))
    conn.execute("DELETE FROM promo_calendar")
    conn.commit()
    conn.close()
    promo.clear_range(scratch, "2026-10-20", "2026-11-11")

    period = promo.create_period(scratch, name="临时活动", start_dt="2026-11-20", end_dt="2026-11-21")
    promo.rebuild_flags(scratch, start="2026-11-20", end="2026-11-21", force=True)
    assert _flags(scratch, "2026-11-20", "2026-11-21")[0][1] == 1

    promo.delete_period(scratch, period["promo_id"])
    assert promo.list_periods(scratch) == []
    assert all(r[1] == 0 for r in _flags(scratch, "2026-11-20", "2026-11-21"))


def test_adopt_existing_groups_consecutive_days(scratch: Path) -> None:
    created = promo.adopt_existing(scratch)
    names = {p["name"] for p in created}
    assert "双 11" in names
    double11 = next(p for p in created if p["name"] == "双 11")
    assert double11["start_dt"] == "2026-10-20" and double11["end_dt"] == "2026-11-11"
    # 再收编一次不重复插
    assert promo.adopt_existing(scratch) == []


def test_import_refreshes_promo_flags(scratch: Path) -> None:
    promo.create_period(scratch, name="导入测试促销", start_dt="2026-11-20", end_dt="2026-11-21")
    rows = [
        {
            "order_id": "PROMO-ORDER-1",
            "dt": "2026-11-20",
            "sku_id": "SKU0001",
            "pay_amount": 99.0,
            "qty": 1,
            "unit_price": 99.0,
            "discount": 0.0,
        }
    ]
    result = store.commit(
        str(scratch),
        "orders",
        rows,
        filename="promo.csv",
        file_sha256="deadbeef",
        report={"rows_total": 1, "verdict": "pass"},
        mapping={"order_id": "order_id", "dt": "dt", "sku_id": "sku_id", "pay_amount": "pay_amount"},
        root=Path(".tmp"),
        confirm_changes=True,
    )
    assert result["promo"] and result["promo"]["rebuilt"] is True
    assert _flags(scratch, "2026-11-20", "2026-11-20")[0][1] == 1


def test_api_crud_and_errors(monkeypatch: pytest.MonkeyPatch, scratch: Path) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("DB_PATH", str(scratch))
    get_settings.cache_clear()

    listed = _call("GET", "/api/promo")
    assert listed.status_code == 200
    assert listed.json()["count"] >= 0

    created = _call(
        "POST",
        "/api/promo",
        json={"name": "接口促销", "start_dt": "2026-11-20", "end_dt": "2026-11-21"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["period"]["days"] == 2
    assert body["sync"]["rebuilt"] is True
    promo_id = body["period"]["promo_id"]

    bad = _call(
        "POST",
        "/api/promo",
        json={"name": "倒置", "start_dt": "2026-11-25", "end_dt": "2026-11-20"},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "invalid_period"

    patched = _call("PATCH", f"/api/promo/{promo_id}", json={"end_dt": "2026-11-23"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["period"]["days"] == 4

    rebuilt = _call("POST", "/api/promo/rebuild", json={})
    assert rebuilt.status_code == 200 and rebuilt.json()["sync"]["rebuilt"] is True

    removed = _call("DELETE", f"/api/promo/{promo_id}")
    assert removed.status_code == 200 and removed.json()["removed"]["promo_id"] == promo_id

    missing = _call("DELETE", "/api/promo/pr_nope")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "promo_not_found"

    get_settings.cache_clear()
