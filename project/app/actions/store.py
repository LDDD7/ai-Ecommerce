"""动作的落库与状态机（spec §9）。

一次动作的完整生命周期：

    drafted ──approve──▶ approved ──execute──▶ executed ──rollback──▶ rolled_back
       │                    │
       ├──reject──▶ rejected └──reject──▶ rejected
       └──execute（仅 risk 0 允许跳过审批）

三条硬规则：
1. **状态迁移白名单**：`schema.TRANSITIONS` 之外的调用一律抛 `ActionError`；
2. **幂等**：同一天、同一 SKU、同一参数的重复提交返回既有动作，不新建、不重复执行；
3. **每一步都写审计**：谁在什么时候把状态从什么改成了什么，前后值都在 `detail_json` 里。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.actions.execute import apply as _apply_action
from app.actions.execute import revert as _revert_action
from app.actions.schema import (
    RISK_LABELS,
    SPECS,
    ActionError,
    TRANSITIONS,
    action_summary,
    check_guardrails,
    classify_risk,
    idempotency_key,
    spec_of,
    validate_params,
)

DEFAULT_STORE_ID = "store_001"
"""schema 缺省值；真实演示库里从 `store_state` 反查（生成器写的是 STORE01）。"""
_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id   TEXT,
    event       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(db_path: str | Path) -> None:
    """老库可能没有 `audit_log`（schema.sql 里是后加的），用 IF NOT EXISTS 补齐。"""
    conn = _connect(db_path)
    try:
        conn.execute(_AUDIT_DDL)
        conn.commit()
    finally:
        conn.close()


def _audit(conn: sqlite3.Connection, action_id: str | None, event: str, actor: str, detail: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO audit_log (action_id, event, actor, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (action_id, event, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), _now()),
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    out = {
        "action_id": row["action_id"],
        "trace_id": row["trace_id"],
        "store_id": row["store_id"],
        "type": row["type"],
        "label": SPECS[row["type"]].label if row["type"] in SPECS else row["type"],
        "sku_id": row["sku_id"],
        "risk_level": row["risk_level"],
        "risk_label": RISK_LABELS.get(row["risk_level"], "待确认"),
        "status": row["status"],
        "idempotency_key": row["idempotency_key"],
        "approver": row["approver"],
        "executed_at": row["executed_at"],
        "rollback_of": row["rollback_of"],
        "created_at": row["created_at"],
    }
    out.update(payload)
    out["summary"] = payload.get("summary") or action_summary(row["type"], payload.get("params") or {}, row["sku_id"])
    return out


def get_action(db_path: str | Path, action_id: str) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM action_log WHERE action_id=?", (action_id,)).fetchone()
        return None if row is None else _row_to_dict(row)
    finally:
        conn.close()


def action_counts(
    db_path: str | Path,
    *,
    type: str | None = None,
    store_id: str | None = None,
) -> dict[str, int]:
    """按状态统计动作条数，不受 limit 截断。

    前端状态胶囊要显示真实计数，不能像 ``len(list_actions(...))`` 那样被默认
    limit 50 截断；这里直接 ``GROUP BY status`` 一次查全。
    """
    sql = "SELECT status, COUNT(*) AS n FROM action_log WHERE 1=1"
    args: list[Any] = []
    if type:
        sql += " AND type=?"
        args.append(type)
    if store_id:
        sql += " AND store_id=?"
        args.append(store_id)
    sql += " GROUP BY status"
    conn = _connect(db_path)
    try:
        return {str(row["status"]): int(row["n"]) for row in conn.execute(sql, args)}
    finally:
        conn.close()


def list_actions(
    db_path: str | Path,
    *,
    status: str | None = None,
    type: str | None = None,
    store_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM action_log WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if type:
        sql += " AND type=?"
        args.append(type)
    if store_id:
        sql += " AND store_id=?"
        args.append(store_id)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    conn = _connect(db_path)
    try:
        return [_row_to_dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _current_price(db_path: str | Path, store_id: str, sku_id: str | None) -> tuple[float | None, float | None]:
    """返回 (当前售价, 成本)：售价优先取 store_state（执行过动作），否则取 dim_sku.price_tag。"""
    conn = _connect(db_path)
    try:
        price = None
        cost = None
        if sku_id:
            row = conn.execute(
                "SELECT value FROM store_state WHERE store_id=? AND sku_id=? AND field='price'",
                (store_id, sku_id),
            ).fetchone()
            if row is not None:
                try:
                    price = float(row["value"])
                except (TypeError, ValueError):
                    price = None
            sku = conn.execute("SELECT price_tag, cost FROM dim_sku WHERE sku_id=?", (sku_id,)).fetchone()
            if sku is not None:
                cost = float(sku["cost"]) if sku["cost"] is not None else None
                if price is None and sku["price_tag"] is not None:
                    price = float(sku["price_tag"])
        return price, cost
    finally:
        conn.close()


def _current_bid(db_path: str | Path, store_id: str, sku_id: str | None) -> float | None:
    """当前出价：只认 store_state.bid。

    没有历史出价时返回 None（而不是拿 cpc 猜）——猜出来的基准会让
    「涨幅是否超 50%」这道护栏形同虚设。
    """
    if not sku_id:
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM store_state WHERE store_id=? AND sku_id=? AND field='bid'",
            (store_id, sku_id),
        ).fetchone()
        if row is None:
            return None
        try:
            return float(row["value"])
        except (TypeError, ValueError):
            return None
    finally:
        conn.close()


def create_action(
    db_path: str | Path,
    *,
    type: str,
    params: dict[str, Any],
    sku_id: str | None = None,
    store_id: str | None = None,
    trace_id: str = "",
    reason: str = "",
    actor: str = "agent",
    day: str | date | None = None,
    effect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """新建草案（drafted）。同幂等键已存在时直接返回既有动作并标 `duplicate=True`。"""
    ensure_tables(db_path)
    store_id = resolve_store_id(db_path, store_id)
    spec = spec_of(type)
    clean = validate_params(type, params)
    price, cost = _current_price(db_path, store_id, sku_id)
    bid = _current_bid(db_path, store_id, sku_id)
    warnings = check_guardrails(type, clean, current_price=price, cost=cost, current_bid=bid)
    risk = classify_risk(type, clean, current_price=price, current_bid=bid)
    key = idempotency_key(type, store_id, sku_id, clean, day=day)

    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM action_log WHERE idempotency_key=?", (key,)).fetchone()
        if row is not None:
            out = _row_to_dict(row)
            out["duplicate"] = True
            return out

        action_id = f"act_{uuid.uuid4().hex[:12]}"
        payload = {
            "params": clean,
            "reason": (reason or "")[:300],
            "warnings": warnings,
            "effect": effect or {},
            "summary": action_summary(type, clean, sku_id),
            "spec": {"label": spec.label, "effect_metric": spec.effect_metric, "store_field": spec.store_field},
            "baseline": {"price": price, "cost": cost, "bid": bid},
        }
        conn.execute(
            """
            INSERT INTO action_log
                (action_id, trace_id, store_id, type, sku_id, payload_json, risk_level, status,
                 idempotency_key, approver, executed_at, rollback_of, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'drafted', ?, NULL, NULL, NULL, ?)
            """,
            (
                action_id,
                trace_id or f"tr_{uuid.uuid4().hex[:8]}",
                store_id,
                type,
                sku_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                risk,
                key,
                _now(),
            ),
        )
        _audit(
            conn,
            action_id,
            "drafted",
            actor,
            {"type": type, "sku_id": sku_id, "params": clean, "risk_level": risk, "warnings": warnings},
        )
        conn.commit()
        row = conn.execute("SELECT * FROM action_log WHERE action_id=?", (action_id,)).fetchone()
        out = _row_to_dict(row)
        out["duplicate"] = False
        return out
    finally:
        conn.close()


def resolve_store_id(db_path: str | Path, store_id: str | None = None) -> str:
    """显式传入优先；否则取 `store_state` 里出现最多的 store_id。

    生成器把状态写在 `STORE01` 下，而接口默认值写的是 `store_001`——
    不反查就会「明明有线上价却回落到标价」，风险分级也跟着失真。
    """
    if store_id:
        return store_id
    try:
        conn = _connect(db_path)
        try:
            # 先看默认店在库里有没有状态：有就是它（生成器与接口都以这个 ID 为准）
            hit = conn.execute(
                "SELECT 1 FROM store_state WHERE store_id=? LIMIT 1", (DEFAULT_STORE_ID,)
            ).fetchone()
            if hit is not None:
                return DEFAULT_STORE_ID
            row = conn.execute(
                "SELECT store_id FROM store_state GROUP BY store_id ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is not None and row["store_id"]:
            return str(row["store_id"])
    except Exception:  # noqa: BLE001 - 反查失败不该让动作失败
        pass
    return DEFAULT_STORE_ID


def _require(db_path: str | Path, action_id: str) -> dict[str, Any]:
    action = get_action(db_path, action_id)
    if action is None:
        raise LookupError(f"动作不存在：{action_id}")
    return action


def _check_transition(action: dict[str, Any], target: str) -> None:
    allowed = TRANSITIONS.get(action["status"], ())
    if target not in allowed:
        raise ActionError(f"动作当前状态是 {action['status']}，不能变更为 {target}")
    if target == "executed" and action["status"] == "drafted" and action["risk_level"] > 0:
        raise ActionError(
            f"该动作风险等级 {action['risk_level']}（{action['risk_label']}），必须先审批再执行"
        )


def approve_action(
    db_path: str | Path,
    action_id: str,
    *,
    approver: str,
    role: str = "manager",
    note: str = "",
) -> dict[str, Any]:
    ensure_tables(db_path)
    action = _require(db_path, action_id)
    _check_transition(action, "approved")
    if not str(approver or "").strip():
        raise ActionError("审批人不能为空")
    if action["risk_level"] == 2 and role not in ("manager", "owner"):
        raise ActionError("高风险动作需要店长（manager）或店主（owner）审批")

    conn = _connect(db_path)
    try:
        claim = conn.execute(
            "UPDATE action_log SET status='approved', approver=? WHERE action_id=? AND status='drafted'",
            (str(approver)[:64], action_id),
        )
        if claim.rowcount != 1:
            conn.rollback()
            raise ActionError("动作已被其它请求处理，请刷新后再看")
        _audit(
            conn,
            action_id,
            "approved",
            str(approver)[:64],
            {"role": role, "note": note[:300], "risk_level": action["risk_level"]},
        )
        conn.commit()
    finally:
        conn.close()
    out = _require(db_path, action_id)
    out["duplicate"] = False
    return out


def reject_action(db_path: str | Path, action_id: str, *, actor: str, reason: str = "") -> dict[str, Any]:
    ensure_tables(db_path)
    if not str(actor or "").strip():
        raise ActionError("操作人不能为空")
    action = _require(db_path, action_id)
    _check_transition(action, "rejected")
    conn = _connect(db_path)
    try:
        claim = conn.execute(
            "UPDATE action_log SET status='rejected' WHERE action_id=? AND status IN ('drafted','approved')",
            (action_id,),
        )
        if claim.rowcount != 1:
            conn.rollback()
            raise ActionError("动作已被其它请求处理，请刷新后再看")
        _audit(conn, action_id, "rejected", str(actor)[:64], {"reason": reason[:300]})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _require(db_path, action_id)


def execute_action(db_path: str | Path, action_id: str, *, actor: str = "system") -> dict[str, Any]:
    """执行：状态迁移 + 写 `store_state` 在同一个事务里完成。

    `UPDATE ... WHERE status IN (...)` 的 rowcount 就是并发闸门：两个请求同时点“执行”，
    只有一个能把 status 抢成 executed，另一个拿不到行、抛错退出，
    不会出现「状态已变两次、快照记录的是改后的值」。
    """
    ensure_tables(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM action_log WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            raise LookupError(f"动作不存在：{action_id}")
        action = _row_to_dict(row)
        _check_transition(action, "executed")

        claim = conn.execute(
            "UPDATE action_log SET status='executed', executed_at=? WHERE action_id=? AND status IN ('approved','drafted')",
            (_now(), action_id),
        )
        if claim.rowcount != 1:
            raise ActionError("动作已被其它请求处理，请刷新后再看")

        result = _apply_action(db_path, action, conn=conn)
        payload = {
            "params": action.get("params"),
            "reason": action.get("reason"),
            "warnings": action.get("warnings") or [],
            "effect": action.get("effect") or {},
            "summary": action.get("summary"),
            "spec": action.get("spec"),
            "baseline": action.get("baseline"),
            "snapshot": result.get("snapshot"),
            "after": result.get("after"),
            "diff": {
                "field": result.get("field"),
                "before": (result.get("snapshot") or {}).get("value") if result.get("snapshot") else None,
                "after": result.get("after"),
            },
        }
        conn.execute(
            "UPDATE action_log SET payload_json=? WHERE action_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), action_id),
        )
        _audit(
            conn,
            action_id,
            "executed",
            str(actor)[:64],
            {"applied": result.get("applied"), "diff": payload["diff"]},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _require(db_path, action_id)


def rollback_action(db_path: str | Path, action_id: str, *, actor: str, reason: str = "") -> dict[str, Any]:
    ensure_tables(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM action_log WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            raise LookupError(f"动作不存在：{action_id}")
        action = _row_to_dict(row)
        _check_transition(action, "rolled_back")

        claim = conn.execute(
            "UPDATE action_log SET status='rolled_back' WHERE action_id=? AND status='executed'",
            (action_id,),
        )
        if claim.rowcount != 1:
            raise ActionError("动作当前不在可回滚状态，请刷新后再看")

        revert = _revert_action(db_path, action.get("snapshot"), conn=conn)
        _audit(conn, action_id, "rolled_back", str(actor)[:64], {"reason": reason[:300], "revert": revert})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    out = _require(db_path, action_id)
    out["revert"] = revert
    return out


def audit_events(
    db_path: str | Path,
    *,
    action_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_tables(db_path)
    sql = "SELECT * FROM audit_log"
    args: list[Any] = []
    if action_id:
        sql += " WHERE action_id=?"
        args.append(action_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, args).fetchall()
        return [
            {
                "id": r["id"],
                "action_id": r["action_id"],
                "event": r["event"],
                "actor": r["actor"],
                "detail": json.loads(r["detail_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def trace_events(db_path: str | Path, trace_id: str) -> list[dict[str, Any]]:
    """按 trace 聚合动作与审计，便于 `GET /api/trace/{id}` 一次看全。

    直接按 `trace_id` 查 `action_log`（而不是从最近 200 条里过滤），
    否则动作一多，早期 trace 就查不到了。
    """
    ensure_tables(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT action_id FROM action_log WHERE trace_id=? ORDER BY created_at, rowid",
            (trace_id,),
        ).fetchall()
        ids = [r["action_id"] for r in rows]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        audit_rows = conn.execute(
            f"SELECT * FROM audit_log WHERE action_id IN ({marks}) ORDER BY id",
            ids,
        ).fetchall()
        return [
            {
                "id": r["id"],
                "action_id": r["action_id"],
                "event": r["event"],
                "actor": r["actor"],
                "detail": json.loads(r["detail_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in audit_rows
        ]
    finally:
        conn.close()
