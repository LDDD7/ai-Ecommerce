"""HTTP 层测试（httpx.AsyncClient + ASGITransport；starlette 1.6 已弃用 TestClient）。"""

from __future__ import annotations

import asyncio
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.main import app


def call(method: str, path: str, json: Any = None):
    async def _run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=json)

    return asyncio.run(_run())


def test_health_ok():
    r = call("GET", "/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"]["exists"] is True
    assert body["metrics_count"] == 24
    assert body["db"]["orders"] > 0


def test_catalog_public_shape():
    r = call("GET", "/api/metrics/catalog")
    assert r.status_code == 200
    body = r.json()
    assert len(body["metrics"]) == 24
    gmv = next(m for m in body["metrics"] if m["name"] == "gmv")
    assert gmv["unit"] == "currency"
    assert "dt" in gmv["dimensions"]
    assert body["limits"]["max_rows"] == 5000


def test_query_ok():
    payload = {
        "spec": {
            "metrics": ["gmv", "paid_orders"],
            "dimensions": ["dt"],
            "time": {"start": "2026-11-10", "end": "2026-11-18"},
            "order_by": [{"field": "dt", "dir": "asc"}],
            "limit": 20,
        }
    }
    r = call("POST", "/api/query", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["dt", "gmv", "paid_orders"]
    assert len(body["rows"]) == 9
    assert body["meta"]["row_count"] == 9
    assert body["spec"]["metrics"] == ["gmv", "paid_orders"]


def test_query_unknown_metric_400():
    r = call("POST", "/api/query", json={"spec": {"metrics": ["nope"]}})
    assert r.status_code == 400


def test_query_bad_dimension_400():
    r = call(
        "POST",
        "/api/query",
        json={"spec": {"metrics": ["dsr"], "dimensions": ["channel_id"]}},
    )
    assert r.status_code == 400
    assert "dimension" in r.json()["detail"]["message"].lower() or r.json()["detail"]["error"]


def test_query_validation_error_400():
    r = call("POST", "/api/query", json={"spec": {"metrics": []}})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_spec"


def test_diagnose_default_shape():
    """不传参数：用配置里的 demo_as_of，返回卡 + 分解 + 漏斗。"""
    from app.core.config import get_settings

    r = call("POST", "/api/diagnose", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["as_of"] == get_settings().demo_as_of
    assert body["window"]["start"] < body["window"]["end"]
    assert isinstance(body["cards"], list)
    if body["cards"]:
        card = body["cards"][0]
        for key in ("code", "title", "severity", "confidence", "evidence", "narrative"):
            assert key in card
        assert len(card["evidence"]) <= 4
    assert body["gmv"]["summary"]
    assert body["funnel"]["summary"]
    assert body["meta"]["engine"] == "rules-v1"


def test_diagnose_get_equivalent():
    # 测试库只有 30 天，绝对日期会被 resolve_as_of 夹到数据范围内，所以这里只验形状。
    r = call("GET", "/api/diagnose?window_days=7&max_cards=2")
    assert r.status_code == 200
    body = r.json()
    assert body["window"]["start"] and body["window"]["end"]
    assert len(body["cards"]) <= 2


def test_diagnose_bad_date_400():
    r = call("POST", "/api/diagnose", json={"as_of": "2026/09/30"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_date"


def test_diagnose_bad_window_400():
    r = call("POST", "/api/diagnose", json={"window_days": 90})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_window"
