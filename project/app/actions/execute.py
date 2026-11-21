"""mock 执行器：把动作落到 `store_state`，并记下前后值以便回滚（spec §9）。

这里刻意不做任何真实平台调用：
- 平台 API 需要凭证与审核，演示环境不可能有；
- 但**状态迁移与前后值快照**是真的，所以「执行了 → 状态变了 → 回滚能还原」
  这条链路可以完整演示。

约定：`store_state` 的主键是 `(store_id, sku_id, field)`，一个字段一行；
写入前先把旧值塞进动作的 `snapshot`，回滚时按 snapshot 恢复（原本没有就删掉）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.actions.schema import ActionError, spec_of


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_state_conn(conn: sqlite3.Connection, store_id: str, sku_id: str, field_name: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM store_state WHERE store_id=? AND sku_id=? AND field=?",
        (store_id, sku_id, field_name),
    ).fetchone()
    return None if row is None else str(row["value"])


def _write_state_conn(
    conn: sqlite3.Connection, store_id: str, sku_id: str, field_name: str, value: str
) -> None:
    conn.execute(
        """
        INSERT INTO store_state (store_id, sku_id, field, value, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(store_id, sku_id, field) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (store_id, sku_id, field_name, value, _now()),
    )


def _delete_state_conn(conn: sqlite3.Connection, store_id: str, sku_id: str, field_name: str) -> None:
    conn.execute(
        "DELETE FROM store_state WHERE store_id=? AND sku_id=? AND field=?",
        (store_id, sku_id, field_name),
    )


def read_state(db_path: str | Path, store_id: str, sku_id: str, field_name: str) -> str | None:
    conn = _connect(db_path)
    try:
        return _read_state_conn(conn, store_id, sku_id, field_name)
    finally:
        conn.close()


def _write_state(db_path: str | Path, store_id: str, sku_id: str, field_name: str, value: str) -> None:
    conn = _connect(db_path)
    try:
        _write_state_conn(conn, store_id, sku_id, field_name, value)
        conn.commit()
    finally:
        conn.close()


def _delete_state(db_path: str | Path, store_id: str, sku_id: str, field_name: str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "DELETE FROM store_state WHERE store_id=? AND sku_id=? AND field=?",
            (store_id, sku_id, field_name),
        )
        conn.commit()
    finally:
        conn.close()


def next_value(action_type: str, params: dict[str, Any]) -> str:
    """动作执行后 `store_state.value` 应该变成什么。"""
    if action_type == "adjust_price":
        return f"{float(params['new_price']):.2f}"
    if action_type == "adjust_bid":
        return f"{float(params['bid']):.3f}"
    if action_type == "swap_creative":
        return str(params["creative_id"])
    if action_type == "issue_coupon":
        return json.dumps(
            {
                "amount": params["amount"],
                "threshold": params.get("threshold"),
                "quota": params.get("quota"),
                "budget": params.get("budget"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    if action_type == "restock_alert":
        return json.dumps(
            {"days_of_stock": params.get("days_of_stock"), "supplier": params.get("supplier")},
            ensure_ascii=False,
            sort_keys=True,
        )
    raise ActionError(f"未知动作类型：{action_type}")


def preview(db_path: str | Path, action: dict[str, Any]) -> dict[str, Any]:
    """dry-run：给出 before/after 与副作用说明，不写任何表。

    与 `create_action` 用同一套校验和护栏——预览不该比创建更宽松，
    否则「预览通过、创建被拒」会让人怀疑护栏是不是随口说的。
    """
    from app.actions.schema import check_guardrails, validate_params

    spec = spec_of(action["type"])
    store_id = action.get("store_id") or "store_001"
    sku_id = action.get("sku_id") or ""
    params = validate_params(action["type"], action.get("params") or {})
    before = read_state(db_path, store_id, sku_id, spec.store_field) if sku_id else None
    current_price = None
    current_bid = None
    if sku_id:
        if action["type"] == "adjust_price":
            current_price = float(before) if before not in (None, "") else _price_tag(db_path, sku_id)
        elif action["type"] == "adjust_bid":
            current_bid = float(before) if before not in (None, "") else None
    warnings = check_guardrails(
        action["type"],
        params,
        current_price=current_price,
        cost=_cost(db_path, sku_id),
        current_bid=current_bid,
    )
    return {
        "type": action["type"],
        "label": spec.label,
        "store_field": spec.store_field,
        "sku_id": sku_id or None,
        "before": before,
        "after": next_value(action["type"], params),
        "effect_metric": spec.effect_metric,
        "side_effects": _side_effects(action["type"], params),
        "warnings": warnings,
        "writes_state": bool(sku_id),
    }


def _price_tag(db_path: str | Path, sku_id: str) -> float | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT price_tag FROM dim_sku WHERE sku_id=?", (sku_id,)).fetchone()
        return None if row is None or row["price_tag"] is None else float(row["price_tag"])
    finally:
        conn.close()


def _cost(db_path: str | Path, sku_id: str) -> float | None:
    if not sku_id:
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT cost FROM dim_sku WHERE sku_id=?", (sku_id,)).fetchone()
        return None if row is None or row["cost"] is None else float(row["cost"])
    finally:
        conn.close()


def _side_effects(action_type: str, params: dict[str, Any]) -> list[str]:
    if action_type == "adjust_price":
        return ["前台售价立即生效", "历史订单不追溯", "可能触发平台改价频次限制"]
    if action_type == "adjust_bid":
        return ["次日投放预算按新出价重算", "当前计划不暂停"]
    if action_type == "swap_creative":
        return ["主图替换后 24 小时内的点击率不可与前一日直接比较"]
    if action_type == "issue_coupon":
        return [f"发放 {params.get('quota', '默认')} 张券", "券成本计入营销费用"]
    if action_type == "restock_alert":
        return ["只发提醒，不改前台库存"]
    return []


def apply(db_path: str | Path, action: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """执行：写 `store_state` 并返回快照（供回滚）。

    传 `conn` 时复用调用方的事务：状态迁移与状态写入必须同生共死，
    否则「已写状态但没改 status」会让重试把改后的值当成 before 快照。
    """
    spec = spec_of(action["type"])
    store_id = action.get("store_id") or "store_001"
    sku_id = action.get("sku_id") or ""
    params = action.get("params") or {}
    if not sku_id:
        # 全店级动作目前只支持提醒类，避免一次改 300 个 SKU
        if action["type"] != "restock_alert":
            raise ActionError("全店级动作只支持 restock_alert；其余请指定 sku_id")
        return {"snapshot": None, "after": None, "field": spec.store_field, "applied": False}

    field_name = spec.store_field
    after = next_value(action["type"], params)
    own = conn is None
    connection = conn or _connect(db_path)
    try:
        before = _read_state_conn(connection, store_id, sku_id, field_name)
        _write_state_conn(connection, store_id, sku_id, field_name, after)
        if own:
            connection.commit()
    finally:
        if own:
            connection.close()
    return {
        "snapshot": {"store_id": store_id, "sku_id": sku_id, "field": field_name, "value": before},
        "after": after,
        "field": field_name,
        "applied": True,
    }


def revert(
    db_path: str | Path,
    snapshot: dict[str, Any] | None,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """回滚：按快照恢复；原本没有该字段就删掉。同样支持复用调用方事务。"""
    if not snapshot:
        return {"reverted": False, "reason": "该动作没有写状态，无需回滚"}
    store_id = snapshot["store_id"]
    sku_id = snapshot["sku_id"]
    field_name = snapshot["field"]
    value = snapshot.get("value")
    own = conn is None
    connection = conn or _connect(db_path)
    try:
        current = _read_state_conn(connection, store_id, sku_id, field_name)
        if value is None:
            _delete_state_conn(connection, store_id, sku_id, field_name)
        else:
            _write_state_conn(connection, store_id, sku_id, field_name, str(value))
        if own:
            connection.commit()
    finally:
        if own:
            connection.close()
    return {"reverted": True, "from": current, "to": value, "field": field_name}
