"""动作闭环的加固测试：并发抢占、preview 与 create 一致、出价基准、trace 直查。

这些点来自状态机测试那轮的自查——都是「演示时不会暴露、真用起来会咬人」的地方：
- 两个请求同时点执行，会不会都成功；
- 预览能不能放行一个创建时必然被拒的参数；
- 出价风险有没有真的拿到当前出价；
- 动作多了以后老 trace 还能不能查。

注意：用例跑在共享小库上，SKU 与参数都带本次运行的随机后缀，
否则上一轮留下的 `action_log` 会命中幂等键，拿回一条已执行的动作。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from app.actions import (
    ActionError,
    approve_action,
    create_action,
    execute_action,
    get_action,
    list_actions,
    preview_action,
    reject_action,
    rollback_action,
    trace_events,
)

RUN = uuid.uuid4().hex[:6]


def sid(name: str) -> str:
    return f"zzh_{name}_{RUN}"


def _price_tag(db_path, sku_id: str) -> float:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT price_tag FROM dim_sku WHERE sku_id=?", (sku_id,)).fetchone()
        return float(row[0])
    finally:
        conn.close()


def _seed_bid(db_path, sku_id: str, bid: float) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO store_state (store_id, sku_id, field, value, updated_at) "
            "VALUES ('store_001', ?, 'bid', ?, '2026-11-20T00:00:00+08:00') "
            "ON CONFLICT(store_id, sku_id, field) DO UPDATE SET value=excluded.value",
            (sku_id, f"{bid:.3f}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_bid_risk_uses_current_bid_from_store_state(db_path):
    sku = sid("bid1")
    _seed_bid(db_path, sku, 2.0)
    action = create_action(db_path, type="adjust_bid", params={"bid": 2.5}, sku_id=sku)
    assert action["risk_level"] == 2  # +25% 出价，有基准就该升级
    assert action["baseline"]["bid"] == 2.0


def test_bid_guardrail_blocks_huge_rise(db_path):
    sku = sid("bid2")
    _seed_bid(db_path, sku, 2.0)
    with pytest.raises(ActionError, match="出价涨幅"):
        create_action(db_path, type="adjust_bid", params={"bid": 3.5}, sku_id=sku)


def test_execute_is_claimed_once(db_path):
    action = create_action(db_path, type="restock_alert", params={"days_of_stock": 3}, sku_id=sid("exec1"))
    first = execute_action(db_path, action["action_id"], actor="tester")
    assert first["status"] == "executed"
    with pytest.raises(ActionError):
        execute_action(db_path, action["action_id"], actor="tester")
    assert get_action(db_path, action["action_id"])["status"] == "executed"


def test_rollback_is_claimed_once(db_path):
    action = create_action(
        db_path, type="adjust_price", params={"new_price": 95.0}, sku_id=sid("rb1")
    )
    approve_action(db_path, action["action_id"], approver="alice")
    execute_action(db_path, action["action_id"], actor="alice")
    rollback_action(db_path, action["action_id"], actor="alice")
    with pytest.raises(ActionError):
        rollback_action(db_path, action["action_id"], actor="alice")


def test_preview_rejects_what_create_rejects(db_path):
    with pytest.raises(ActionError):
        preview_action(db_path, {"type": "adjust_price", "sku_id": "SKU0001", "params": {}})
    with pytest.raises(ActionError, match="降幅"):
        preview_action(
            db_path, {"type": "adjust_price", "sku_id": "SKU0001", "params": {"new_price": 40.0}}
        )


def test_preview_reports_warnings(db_path):
    base = _price_tag(db_path, "SKU0001")
    target = round(base * 0.94, 2)
    out = preview_action(
        db_path, {"type": "adjust_price", "sku_id": "SKU0001", "params": {"new_price": target}}
    )
    assert out["after"] == f"{target:.2f}"
    assert out["warnings"], "变动 ≥5% 应当给出价格带调整提醒"


def test_reject_requires_actor(db_path):
    action = create_action(
        db_path, type="adjust_price", params={"new_price": 95.0}, sku_id=sid("rej1")
    )
    with pytest.raises(ActionError, match="操作人"):
        reject_action(db_path, action["action_id"], actor="  ")


def test_trace_events_reads_by_trace_id(db_path):
    trace = f"tr_zzh_{RUN}"
    a = create_action(
        db_path, type="restock_alert", params={"days_of_stock": 2}, sku_id=sid("tr1"), trace_id=trace
    )
    b = create_action(
        db_path, type="restock_alert", params={"days_of_stock": 2}, sku_id=sid("tr2"), trace_id=trace
    )
    execute_action(db_path, a["action_id"], actor="tester")
    execute_action(db_path, b["action_id"], actor="tester")
    events = trace_events(db_path, trace)
    assert {e["action_id"] for e in events} == {a["action_id"], b["action_id"]}
    assert [e["event"] for e in events].count("executed") == 2
    assert len(events) == 4


def test_list_actions_is_stable_for_same_second(db_path):
    ids = []
    for i in range(3):
        item = create_action(
            db_path,
            type="restock_alert",
            params={"days_of_stock": i + 1},
            sku_id=sid(f"st{i}"),
        )
        ids.append(item["action_id"])
    first = [a["action_id"] for a in list_actions(db_path, limit=50)]
    second = [a["action_id"] for a in list_actions(db_path, limit=50)]
    assert first == second
    assert first[0] == ids[-1]
