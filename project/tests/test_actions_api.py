"""动作闭环的 HTTP 层测试：12 个端点的形状、错误码与状态迁移。"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

import httpx

from app.main import create_app

RUN = uuid.uuid4().hex[:6]


def _call(method: str, path: str, **kwargs):
    async def go():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(go())


def _price_tag(db_path, sku_id: str = "SKU0001") -> float:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT price_tag FROM dim_sku WHERE sku_id=?", (sku_id,)).fetchone()
        return float(row[0])
    finally:
        conn.close()


def _sku(name: str) -> str:
    return f"zza_{name}_{RUN}"


def test_actions_types():
    res = _call("GET", "/api/actions/types")
    assert res.status_code == 200
    body = res.json()
    types = {t["type"] for t in body["types"]}
    assert types == {"adjust_price", "adjust_bid", "swap_creative", "issue_coupon", "restock_alert"}
    for item in body["types"]:
        assert {"label", "required", "store_field", "risk_note"} <= set(item)


def test_dry_run_preview_and_errors(db_path):
    base = _price_tag(db_path)
    ok = _call(
        "POST",
        "/api/actions/dry-run",
        json={"type": "adjust_price", "sku_id": "SKU0001", "params": {"new_price": round(base * 0.97, 2)}},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["after"] == f"{round(base * 0.97, 2):.2f}"
    assert "risk_level" in body

    bad = _call("POST", "/api/actions/dry-run", json={"type": "adjust_price", "params": {}})
    assert bad.status_code == 400
    assert "必填" in bad.json()["detail"]

    unknown = _call("POST", "/api/actions/dry-run", json={"type": "nope", "params": {}})
    assert unknown.status_code == 400


def test_lifecycle_through_api(db_path):
    sku = _sku("life")
    created = _call(
        "POST",
        "/api/actions",
        json={"type": "restock_alert", "sku_id": sku, "params": {"days_of_stock": 2}},
    )
    assert created.status_code == 200
    action = created.json()
    assert action["status"] == "drafted"
    assert action["risk_level"] == 0
    aid = action["action_id"]

    again = _call(
        "POST",
        "/api/actions",
        json={"type": "restock_alert", "sku_id": sku, "params": {"days_of_stock": 2}},
    )
    assert again.json()["duplicate"] is True

    executed = _call("POST", f"/api/actions/{aid}/execute", json={"actor": "tester"})
    assert executed.status_code == 200
    assert executed.json()["status"] == "executed"

    audit = _call("GET", f"/api/audit?action_id={aid}")
    assert audit.status_code == 200
    events = [e["event"] for e in audit.json()["items"]]
    assert set(events) == {"drafted", "executed"}
    assert events[0] == "executed"  # 审计按时间倒序，最新在前

    trace = _call("GET", f"/api/trace/{action['trace_id']}")
    assert trace.status_code == 200
    body = trace.json()
    assert {"trace_id", "actions", "events", "count"} <= set(body)
    assert body["count"] >= 2


def test_risk2_requires_manager(db_path):
    sku = _sku("risk2")
    base = _price_tag(db_path)
    created = _call(
        "POST",
        "/api/actions",
        json={"type": "adjust_price", "sku_id": sku, "params": {"new_price": round(base * 0.92, 2)}},
    )
    assert created.status_code == 200
    action = created.json()
    assert action["risk_level"] == 2
    aid = action["action_id"]

    staff = _call("POST", f"/api/actions/{aid}/approve", json={"approver": "bob", "role": "staff"})
    assert staff.status_code == 400
    assert "店长" in staff.json()["detail"]

    manager = _call("POST", f"/api/actions/{aid}/approve", json={"approver": "alice", "role": "manager"})
    assert manager.status_code == 200
    assert manager.json()["status"] == "approved"

    executed = _call("POST", f"/api/actions/{aid}/execute", json={"actor": "alice"})
    assert executed.status_code == 200
    assert executed.json()["diff"]["after"] == f"{round(base * 0.92, 2):.2f}"

    rolled = _call("POST", f"/api/actions/{aid}/rollback", json={"actor": "alice", "reason": "demo"})
    assert rolled.status_code == 200
    assert rolled.json()["status"] == "rolled_back"
    assert rolled.json()["revert"]["reverted"] is True


def test_execute_without_approval_is_rejected(db_path):
    sku = _sku("noapprove")
    base = _price_tag(db_path)
    created = _call(
        "POST",
        "/api/actions",
        json={"type": "adjust_price", "sku_id": sku, "params": {"new_price": round(base * 0.98, 2)}},
    )
    aid = created.json()["action_id"]
    res = _call("POST", f"/api/actions/{aid}/execute", json={"actor": "tester"})
    assert res.status_code == 400
    assert "必须先审批" in res.json()["detail"]


def test_missing_action_is_404():
    res = _call("GET", "/api/actions/act_not_exist")
    assert res.status_code == 404
    assert "动作不存在" in res.json()["detail"]


def test_risk2_draft_from_cards(db_path):
    cards = [
        {
            "card_id": "card_x1",
            "code": "R3",
            "title": "热销断货",
            "scope": {"dim": "sku_id", "dim_label": "商品", "dim_value": "SKU0001"},
            "gmv_impact": -1000,
            "confidence": 0.8,
            "narrative": "断货导致成交下滑",
        }
    ]
    playbook = {
        "items": [
            {
                "card_id": "card_x1",
                "code": "R3",
                "title": "热销断货",
                "actions": [
                    {
                        "card_id": "PB-R3-01",
                        "title": "可售不足 3 天，紧急补货",
                        "action_type": "restock_alert",
                        "risk": 0,
                        "expected_effect": {"metric": "stockout_rate", "range": [0.15, 0.35], "window_days": 7},
                    }
                ],
            }
        ]
    }
    res = _call("POST", "/api/actions/draft", json={"cards": cards, "playbook": playbook, "store_id": "store_001"})
    assert res.status_code == 200
    body = res.json()
    assert body["actions"], body
    first = body["actions"][0]
    assert first["type"] in ("restock_alert", "adjust_price", "swap_creative", "issue_coupon", "adjust_bid")
    assert first["status"] == "drafted"


def test_list_actions_filters(db_path):
    res = _call("GET", "/api/actions?status=drafted&limit=5")
    assert res.status_code == 200
    body = res.json()
    # items 受 limit 截断，但 count 是「筛选命中总数」，total 是全量总数
    assert len(body["items"]) <= 5
    assert body["count"] >= len(body["items"])
    assert body["total"] >= body["count"]
    assert all(a["status"] == "drafted" for a in body["items"])
    assert "status_counts" in body
    assert body["status_counts"].get("drafted", 0) == body["count"]


def test_list_actions_counts_follow_type_filter(db_path):
    """状态计数跟随 type 过滤；total 始终是全量口径。"""
    all_body = _call("GET", "/api/actions").json()
    only = _call("GET", "/api/actions?type=restock_alert").json()
    assert only["total"] == all_body["total"]
    assert sum(only["status_counts"].values()) == only["count"]
    assert only["count"] <= all_body["total"]
    assert set(only["status_counts"]).issubset(set(all_body["status_counts"]) | {"drafted", "approved", "executed", "rejected", "rolled_back"})
