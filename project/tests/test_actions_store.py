"""动作闭环（mock）的契约测试。

覆盖四层：
- `schema.py`：参数校验/归一化、风险分级、护栏、幂等键（纯函数，不碰库）；
- `store.py`：落库、幂等去重、状态机、审批角色、回滚、审计；
- `execute.py`：`store_state` 的前后值快照与「原本没有就删除」的回滚语义；
- `preview()`：dry-run 只读，不写 `store_state`、不建动作。

隔离约定：数据库是 session 级共享的，所有落到 `action_log` 的用例都走 `bench`
夹具。每个用例拿到唯一的 SKU/trace 前缀，结束时按 trace 删除动作与审计，
按前缀删除自己写入的 `store_state`，避免污染其他测试。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from app.actions import execute as action_execute
from app.actions import store as action_store
from app.actions.schema import (
    RISK_AUTO,
    RISK_MANAGER,
    RISK_REVIEW,
    ActionError,
    action_summary,
    check_guardrails,
    classify_risk,
    idempotency_key,
    validate_params,
)

DAY = "2026-11-20"


class _Bench:
    """一个用例专属的 store/SKU/trace 命名空间。"""

    def __init__(self, db_path: Path) -> None:
        self.db = db_path
        self.prefix = f"zz_{uuid.uuid4().hex[:10]}"
        self.trace = f"tr_{uuid.uuid4().hex[:10]}"
        self.store_id = "store_test"

    def sku(self, name: str) -> str:
        return f"{self.prefix}_{name}"

    def seed_state(self, sku_id: str, field: str, value: str) -> None:
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                """
                INSERT INTO store_state (store_id, sku_id, field, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(store_id, sku_id, field)
                DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (self.store_id, sku_id, field, value, "2026-11-20T00:00:00+08:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def create(self, **kwargs):
        kwargs.setdefault("trace_id", self.trace)
        kwargs.setdefault("store_id", self.store_id)
        kwargs.setdefault("day", DAY)
        return action_store.create_action(self.db, **kwargs)


@pytest.fixture
def bench(db_path: Path):
    b = _Bench(db_path)
    try:
        yield b
    finally:
        conn = sqlite3.connect(str(db_path))
        try:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT action_id FROM action_log WHERE trace_id=?", (b.trace,)
                )
            ]
            if ids:
                marks = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM audit_log WHERE action_id IN ({marks})", ids)
            conn.execute("DELETE FROM action_log WHERE trace_id=?", (b.trace,))
            conn.execute("DELETE FROM store_state WHERE sku_id LIKE ?", (f"{b.prefix}%",))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def test_validate_params_rejects_unknown_action_type():
    with pytest.raises(ActionError, match="未知动作类型"):
        validate_params("teleport", {})


def test_validate_params_rejects_missing_required():
    with pytest.raises(ActionError, match="缺少必填参数"):
        validate_params("adjust_price", {})
    with pytest.raises(ActionError, match="缺少必填参数"):
        validate_params("issue_coupon", {"threshold": 199})


def test_validate_params_rejects_unknown_fields():
    with pytest.raises(ActionError, match="不支持这些参数"):
        validate_params("adjust_price", {"new_price": 100, "discount": 10})


def test_validate_params_rejects_coupon_amount_over_100():
    # 100 是边界，可以通过；超过 100 必须拒绝
    assert validate_params("issue_coupon", {"amount": 100, "quota": 10})["amount"] == 100.0
    with pytest.raises(ActionError, match="券面额不能超过 100 元"):
        validate_params("issue_coupon", {"amount": 100.01})


def test_validate_params_rejects_coupon_budget_over_5000():
    with pytest.raises(ActionError, match="券总预算不能超过 5000 元"):
        validate_params("issue_coupon", {"amount": 10, "budget": 5000.01})
    # 不给 budget 时按 amount * quota 估算，也不能超过 5000
    with pytest.raises(ActionError, match="券总预算不能超过 5000 元"):
        validate_params("issue_coupon", {"amount": 10, "quota": 501})
    assert validate_params("issue_coupon", {"amount": 10, "budget": 5000})["budget"] == 5000.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", "inf"])
def test_validate_params_rejects_nan_and_inf(bad):
    with pytest.raises(ActionError, match="NaN/Inf"):
        validate_params("adjust_price", {"new_price": bad})


def test_validate_params_normalizes_numbers():
    price = validate_params(
        "adjust_price", {"new_price": "99.994", "price_tag": "12.344"}
    )
    assert price == {"new_price": 99.99, "price_tag": 12.34}

    bid = validate_params(
        "adjust_bid", {"bid": "1.2344", "budget_delta": "12.3444", "plan_id": 123}
    )
    assert bid == {"bid": 1.234, "budget_delta": 12.34, "plan_id": "123"}

    coupon = validate_params(
        "issue_coupon", {"amount": "19.999", "threshold": "199.994", "quota": "100.9"}
    )
    assert coupon == {
        "amount": 20.0,
        "threshold": 199.99,
        "quota": 100,
        "budget": 2000.0,
    }


def test_validate_params_does_not_mutate_input():
    params = {"new_price": "100.006"}
    clean = validate_params("adjust_price", params)
    assert params == {"new_price": "100.006"}
    assert clean == {"new_price": 100.01}


# ---------------------------------------------------------------------------
# classify_risk
# ---------------------------------------------------------------------------


def test_classify_risk_static_actions():
    assert classify_risk("restock_alert", {}) == RISK_AUTO == 0
    assert classify_risk("swap_creative", {"creative_id": "cr_1"}) == RISK_REVIEW == 1


def test_classify_risk_adjust_price_thresholds():
    # 100 为当前价：-3% 单人审；-8% / +12% 店长审
    assert classify_risk("adjust_price", {"new_price": 97}, current_price=100) == 1
    assert classify_risk("adjust_price", {"new_price": 92}, current_price=100) == 2
    assert classify_risk("adjust_price", {"new_price": 112}, current_price=100) == 2
    # 精确边界也按规则走：-5% 与 +10% 都进入 risk 2
    assert classify_risk("adjust_price", {"new_price": 95}, current_price=100) == 2
    assert classify_risk("adjust_price", {"new_price": 110}, current_price=100) == 2


def test_classify_risk_adjust_price_without_current_price_is_manager():
    assert classify_risk("adjust_price", {"new_price": 100}) == RISK_MANAGER == 2
    # price_tag 可以作为兜底基准价
    assert (
        classify_risk("adjust_price", {"new_price": 103, "price_tag": 100}) == RISK_REVIEW
    )


def test_classify_risk_issue_coupon():
    assert classify_risk("issue_coupon", {"amount": 20}) == RISK_REVIEW == 1
    assert classify_risk("issue_coupon", {"amount": 50}) == RISK_REVIEW  # 50 是边界
    assert classify_risk("issue_coupon", {"amount": 50.01}) == RISK_MANAGER
    assert classify_risk("issue_coupon", {"amount": 20, "budget": 2000.01}) == RISK_MANAGER
    # 未给 budget 时按 amount×quota 估（与 validate_params 同口径）：30×100=3000 > 2000
    assert classify_risk("issue_coupon", {"amount": 30, "quota": 100}) == RISK_MANAGER


def test_classify_risk_adjust_bid():
    # +30% 出价 → 店长审；恰好 +20% 仍是单人审
    assert classify_risk("adjust_bid", {"bid": 2.6}, current_bid=2.0) == RISK_MANAGER == 2
    assert classify_risk("adjust_bid", {"bid": 2.4}, current_bid=2.0) == RISK_REVIEW
    # budget_delta 是「加投金额（元）」，不是比例：>200 元才升级
    assert classify_risk("adjust_bid", {"bid": 2.0, "budget_delta": 201}) == RISK_MANAGER
    assert classify_risk("adjust_bid", {"bid": 2.0, "budget_delta": 200}) == RISK_REVIEW


# ---------------------------------------------------------------------------
# check_guardrails
# ---------------------------------------------------------------------------


def test_guardrails_rejects_price_drop_over_30_percent():
    with pytest.raises(ActionError, match="降幅"):
        check_guardrails("adjust_price", {"new_price": 69.9}, current_price=100)


def test_guardrails_rejects_price_rise_over_30_percent():
    with pytest.raises(ActionError, match="涨幅"):
        check_guardrails("adjust_price", {"new_price": 130.1}, current_price=100)


def test_guardrails_rejects_margin_below_floor():
    # 100 元卖、96 元成本 → 毛利率 4% < 5%
    with pytest.raises(ActionError, match="毛利率"):
        check_guardrails("adjust_price", {"new_price": 100}, current_price=100, cost=96)


def test_guardrails_warns_on_five_percent_move():
    assert check_guardrails("adjust_price", {"new_price": 104.9}, current_price=100) == []

    up = check_guardrails("adjust_price", {"new_price": 105}, current_price=100)
    assert len(up) == 1
    assert "价格变动 +5.0%" in up[0]

    down = check_guardrails("adjust_price", {"new_price": 95}, current_price=100)
    assert len(down) == 1
    assert "价格变动 -5.0%" in down[0]


def test_guardrails_rejects_bid_rise_over_50_percent():
    with pytest.raises(ActionError, match="出价涨幅"):
        check_guardrails("adjust_bid", {"bid": 1.51}, current_bid=1.0)
    assert check_guardrails("adjust_bid", {"bid": 1.5}, current_bid=1.0) == []


# ---------------------------------------------------------------------------
# idempotency_key
# ---------------------------------------------------------------------------


def test_idempotency_key_same_inputs_same_day():
    kwargs = dict(
        action_type="issue_coupon",
        store_id="store_001",
        sku_id="sku_1",
        params={"amount": 20, "threshold": 199},
    )
    first = idempotency_key(**kwargs, day="2026-11-20")
    second = idempotency_key(**kwargs, day="2026-11-20")
    assert first == second
    assert len(first) == 64


def test_idempotency_key_changes_by_day():
    kwargs = dict(
        action_type="issue_coupon",
        store_id="store_001",
        sku_id="sku_1",
        params={"amount": 20},
    )
    assert idempotency_key(**kwargs, day="2026-11-20") != idempotency_key(
        **kwargs, day="2026-11-21"
    )


def test_idempotency_key_ignores_param_order():
    common = dict(action_type="issue_coupon", store_id="store_001", sku_id="sku_1")
    first = idempotency_key(
        **common, params={"amount": 20, "threshold": 199}, day=DAY
    )
    second = idempotency_key(
        **common, params={"threshold": 199, "amount": 20}, day=DAY
    )
    assert first == second


def test_idempotency_key_changes_by_sku():
    common = dict(action_type="adjust_price", store_id="store_001", params={"new_price": 99})
    assert idempotency_key(**common, sku_id="sku_1", day=DAY) != idempotency_key(
        **common, sku_id="sku_2", day=DAY
    )


# ---------------------------------------------------------------------------
# create_action / get_action / list_actions
# ---------------------------------------------------------------------------


def test_create_action_persists_drafted(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")

    action = bench.create(type="adjust_price", params={"new_price": 103}, sku_id=sku)

    assert action["status"] == "drafted"
    assert action["duplicate"] is False
    assert action["action_id"].startswith("act_")
    assert len(action["action_id"]) == 16
    assert action["risk_level"] == 1
    assert action["risk_label"] == "需人审"
    assert action["risk_level"] == classify_risk(
        "adjust_price", action["params"], current_price=100.0
    )
    assert action["params"] == {"new_price": 103.0}
    assert action["warnings"] == []
    assert action["summary"] == f"{sku} 调价至 ¥103.00"
    assert action["summary"] == action_summary("adjust_price", {"new_price": 103.0}, sku)
    assert action["sku_id"] == sku
    assert action["store_id"] == bench.store_id
    assert action["trace_id"] == bench.trace
    assert action["created_at"]


def test_create_action_is_idempotent(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")

    first = bench.create(type="adjust_price", params={"new_price": 103}, sku_id=sku)
    second = bench.create(type="adjust_price", params={"new_price": 103}, sku_id=sku)

    assert second["duplicate"] is True
    assert second["action_id"] == first["action_id"]

    conn = sqlite3.connect(str(bench.db))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action_id=?", (first["action_id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_get_action_and_list_actions(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")
    action = bench.create(type="adjust_price", params={"new_price": 103}, sku_id=sku)

    fetched = action_store.get_action(bench.db, action["action_id"])
    assert fetched is not None
    assert fetched["action_id"] == action["action_id"]
    assert fetched["status"] == "drafted"
    assert fetched["summary"] == action["summary"]
    assert fetched["params"] == {"new_price": 103.0}

    drafted = action_store.list_actions(
        bench.db, status="drafted", store_id=bench.store_id
    )
    assert action["action_id"] in [row["action_id"] for row in drafted]
    assert all(row["status"] == "drafted" for row in drafted)
    assert all(row["store_id"] == bench.store_id for row in drafted)


# ---------------------------------------------------------------------------
# 状态机：审批 / 执行 / 拒绝 / 回滚
# ---------------------------------------------------------------------------


def test_execute_requires_approval_for_risk_one(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")
    action = bench.create(type="adjust_price", params={"new_price": 103}, sku_id=sku)
    assert action["risk_level"] == 1

    with pytest.raises(ActionError, match="必须先审批"):
        action_store.execute_action(bench.db, action["action_id"], actor="agent")

    assert action_store.get_action(bench.db, action["action_id"])["status"] == "drafted"
    assert action_execute.read_state(bench.db, bench.store_id, sku, "price") == "100.00"


def test_approve_then_execute_updates_state(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")
    action = bench.create(type="adjust_price", params={"new_price": 105}, sku_id=sku)

    approved = action_store.approve_action(
        bench.db, action["action_id"], approver="alice", role="manager", note="同意"
    )
    assert approved["status"] == "approved"
    assert approved["approver"] == "alice"

    executed = action_store.execute_action(bench.db, action["action_id"], actor="alice")
    assert executed["status"] == "executed"
    assert executed["executed_at"]
    assert executed["snapshot"]["value"] == "100.00"
    assert executed["diff"] == {
        "field": "price",
        "before": "100.00",
        "after": "105.00",
    }
    assert action_execute.read_state(bench.db, bench.store_id, sku, "price") == "105.00"


def test_risk_two_requires_manager_role(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")
    action = bench.create(type="adjust_price", params={"new_price": 92}, sku_id=sku)
    assert action["risk_level"] == 2

    with pytest.raises(ActionError, match="店长"):
        action_store.approve_action(
            bench.db, action["action_id"], approver="staff_bob", role="staff"
        )
    assert action_store.get_action(bench.db, action["action_id"])["status"] == "drafted"

    approved = action_store.approve_action(
        bench.db, action["action_id"], approver="mgr_bob", role="manager"
    )
    assert approved["status"] == "approved"
    assert approved["approver"] == "mgr_bob"


def test_reject_blocks_later_approval(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")
    action = bench.create(type="adjust_price", params={"new_price": 103}, sku_id=sku)

    rejected = action_store.reject_action(
        bench.db, action["action_id"], actor="carol", reason="不划算"
    )
    assert rejected["status"] == "rejected"

    with pytest.raises(ActionError, match="不能变更为 approved"):
        action_store.approve_action(
            bench.db, action["action_id"], approver="alice", role="manager"
        )
    with pytest.raises(ActionError, match="不能变更为 executed"):
        action_store.execute_action(bench.db, action["action_id"], actor="alice")

    assert action_store.get_action(bench.db, action["action_id"])["status"] == "rejected"


def test_rollback_restores_previous_value(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")
    action = bench.create(type="adjust_price", params={"new_price": 95}, sku_id=sku)
    action_store.approve_action(
        bench.db, action["action_id"], approver="alice", role="manager"
    )
    action_store.execute_action(bench.db, action["action_id"], actor="alice")
    assert action_execute.read_state(bench.db, bench.store_id, sku, "price") == "95.00"

    rolled = action_store.rollback_action(
        bench.db, action["action_id"], actor="alice", reason="误操作"
    )
    assert rolled["status"] == "rolled_back"
    assert rolled["revert"] == {
        "reverted": True,
        "from": "95.00",
        "to": "100.00",
        "field": "price",
    }
    assert action_execute.read_state(bench.db, bench.store_id, sku, "price") == "100.00"


def test_rollback_deletes_field_that_did_not_exist(bench):
    sku = bench.sku("creative")
    assert action_execute.read_state(bench.db, bench.store_id, sku, "creative") is None
    action = bench.create(
        type="swap_creative", params={"creative_id": "cr_new_001"}, sku_id=sku
    )
    assert action["risk_level"] == 1

    action_store.approve_action(
        bench.db, action["action_id"], approver="alice", role="manager"
    )
    action_store.execute_action(bench.db, action["action_id"], actor="alice")
    assert (
        action_execute.read_state(bench.db, bench.store_id, sku, "creative")
        == "cr_new_001"
    )

    rolled = action_store.rollback_action(
        bench.db, action["action_id"], actor="alice", reason="撤回素材"
    )
    assert rolled["status"] == "rolled_back"
    assert rolled["revert"]["to"] is None
    assert action_execute.read_state(bench.db, bench.store_id, sku, "creative") is None


# ---------------------------------------------------------------------------
# 审计与 trace
# ---------------------------------------------------------------------------


def test_audit_events_record_lifecycle(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")
    action = bench.create(type="adjust_price", params={"new_price": 103}, sku_id=sku)
    action_store.approve_action(
        bench.db, action["action_id"], approver="alice", role="manager", note="ok"
    )
    action_store.execute_action(bench.db, action["action_id"], actor="alice")
    action_store.rollback_action(
        bench.db, action["action_id"], actor="alice", reason="回到原价"
    )

    events = action_store.audit_events(bench.db, action_id=action["action_id"])
    ordered = list(reversed(events))

    assert [e["event"] for e in ordered] == [
        "drafted",
        "approved",
        "executed",
        "rolled_back",
    ]
    assert [e["actor"] for e in ordered] == ["agent", "alice", "alice", "alice"]
    assert all(e["action_id"] == action["action_id"] for e in ordered)
    assert all(e["created_at"] for e in ordered)
    assert all(len(e["created_at"]) >= 19 for e in ordered)
    assert ordered[0]["detail"]["risk_level"] == 1
    assert ordered[1]["detail"]["role"] == "manager"
    assert ordered[2]["detail"]["diff"] == {
        "field": "price",
        "before": "100.00",
        "after": "103.00",
    }
    assert ordered[3]["detail"]["revert"]["to"] == "100.00"


def test_trace_events_aggregates_actions(bench):
    first = bench.create(
        type="restock_alert", params={"days_of_stock": 3, "supplier": "sup_a"}
    )
    second = bench.create(
        type="restock_alert", params={"days_of_stock": 5, "supplier": "sup_b"}
    )
    assert first["risk_level"] == 0
    assert second["risk_level"] == 0

    for action in (first, second):
        executed = action_store.execute_action(
            bench.db, action["action_id"], actor="system"
        )
        assert executed["status"] == "executed"
        assert executed["executed_at"]

    events = action_store.trace_events(bench.db, bench.trace)

    assert len(events) == 4
    assert {e["action_id"] for e in events} == {
        first["action_id"],
        second["action_id"],
    }
    assert [e["event"] for e in events].count("drafted") == 2
    assert [e["event"] for e in events].count("executed") == 2
    assert {e["actor"] for e in events} == {"agent", "system"}
    assert events == sorted(events, key=lambda e: e["id"])


# ---------------------------------------------------------------------------
# preview：dry-run 不落库
# ---------------------------------------------------------------------------


def test_preview_reports_before_after_without_writing(bench):
    sku = bench.sku("price")
    bench.seed_state(sku, "price", "100.00")

    action = {
        "type": "adjust_price",
        "store_id": bench.store_id,
        "sku_id": sku,
        "params": {"new_price": 90},
    }
    preview = action_execute.preview(bench.db, action)

    assert preview["before"] == "100.00"
    assert preview["after"] == "90.00"
    assert preview["store_field"] == "price"
    assert preview["label"] == "调价"
    assert preview["effect_metric"] == "cvr"
    assert preview["writes_state"] is True
    assert len(preview["side_effects"]) == 3
    assert action_execute.read_state(bench.db, bench.store_id, sku, "price") == "100.00"

    conn = sqlite3.connect(str(bench.db))
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE sku_id=?", (sku,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 0

    # 全店级提醒没有 sku，dry-run 明确标注「不写状态」
    global_preview = action_execute.preview(
        bench.db,
        {
            "type": "restock_alert",
            "store_id": bench.store_id,
            "sku_id": None,
            "params": {"days_of_stock": 3},
        },
    )
    assert global_preview["before"] is None
    assert global_preview["writes_state"] is False
    assert global_preview["side_effects"] == ["只发提醒，不改前台库存"]
