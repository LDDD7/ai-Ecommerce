"""动作评测套件（T7-C）。

15 条用例：

- ``A01``–``A10``：五类动作各 2 条正常链路。每条都走
  ``create -> approve（risk>0） -> execute -> 断言 store_state -> rollback``，
  回滚后该 SKU 的状态必须回到动作前（本套件在用例前后都会清掉该 SKU 的
  ``action_log`` / ``store_state``，所以正常链路等价于「写入后再删掉」）。
- ``A11``–``A15``：五条护栏拒绝。分别覆盖价格涨幅、出价涨幅、券面额、
  缺少必填参数、未知动作类型；断言抛 ``ActionError`` 且文案非空。

所有写操作都发生在 ``shutil.copy2`` 出来的临时库上，传入的 ``db_path`` 只读；
临时目录放在 ``project/.tmp/eval_action/<uuid>/``（本机 %TEMP% ACL 异常，不能用
默认临时目录），跑完即删。
"""

from __future__ import annotations

import shutil
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from app.actions import (
    ActionError,
    approve_action,
    create_action,
    execute_action,
    rollback_action,
)
from app.actions.execute import next_value
from app.eval.types import Case, CaseResult, SuiteResult

SUITE = "action"
STORE_ID = "store_001"

_ROOT = Path(__file__).resolve().parents[3]
_TMP_ROOT = _ROOT / ".tmp" / "eval_action"


# ---------------------------------------------------------------------------
# 只读读取：生成用例
# ---------------------------------------------------------------------------


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """只读连接；``cases()`` 绝不碰传入的库。"""
    path = Path(db_path).as_posix()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_store_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT 1 FROM store_state WHERE store_id=? LIMIT 1", (STORE_ID,)).fetchone()
    if row is not None:
        return STORE_ID
    row = conn.execute(
        "SELECT store_id FROM store_state GROUP BY store_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    return str(row["store_id"]) if row is not None and row["store_id"] else STORE_ID


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _load_inputs(db_path: str | Path) -> tuple[list[sqlite3.Row], dict[str, dict[str, str]]]:
    with closing(_connect_ro(db_path)) as conn:
        rows = conn.execute(
            "SELECT sku_id, title, price_tag, cost, supplier FROM dim_sku ORDER BY sku_id"
        ).fetchall()
        store_id = _resolve_store_id(conn)
        state_rows = conn.execute(
            "SELECT sku_id, field, value FROM store_state WHERE store_id=?", (store_id,)
        ).fetchall()
    state: dict[str, dict[str, str]] = {}
    for row in state_rows:
        state.setdefault(str(row["sku_id"]), {})[str(row["field"])] = str(row["value"])
    return rows, state


def _current_price(row: sqlite3.Row, state: dict[str, dict[str, str]]) -> float:
    current = _as_float(state.get(str(row["sku_id"]), {}).get("price"))
    if current is not None:
        return current
    tag = _as_float(row["price_tag"])
    if tag is None:
        raise ActionError(f"{row['sku_id']} 没有可用的标价，无法生成调价用例")
    return tag


def _bid_value(current: float | None) -> float:
    """默认出价 1.20；只有现价太低会让 +50% 护栏误伤时才按比例降一点。"""
    if current is not None and (1.2 - current) / current > 0.50:
        return round(current * 1.10, 3)
    return 1.2


def _normal_case(
    case_id: str,
    *,
    type: str,
    params: dict[str, Any],
    sku_id: str,
    label: str,
    field: str,
) -> Case:
    return Case(
        case_id=case_id,
        suite=SUITE,
        payload={
            "label": f"{label} · {sku_id}",
            "kind": "create",
            "type": type,
            "params": params,
            "sku_id": sku_id,
            "expect": {"field": field, "next_value": next_value(type, params)},
        },
        note="正常链路：create → approve（risk>0）→ execute → rollback",
    )


def _guardrail_case(
    case_id: str,
    *,
    type: str,
    params: dict[str, Any],
    sku_id: str,
    label: str,
    contains: str,
) -> Case:
    return Case(
        case_id=case_id,
        suite=SUITE,
        payload={
            "label": f"{label} · {sku_id}",
            "kind": "guardrail",
            "type": type,
            "params": params,
            "sku_id": sku_id,
            "expect": {"raises": "ActionError", "contains": contains},
        },
        note="护栏拒绝：create_action 必须抛 ActionError 且文案非空",
    )


def cases(db_path: str | Path) -> list[Case]:
    """恰好 15 条：10 条正常 + 5 条护栏拒绝。"""
    rows, state = _load_inputs(db_path)
    if not rows:
        raise ActionError("dim_sku 没有数据，无法生成动作用例")

    def pick(index: int) -> sqlite3.Row:
        return rows[index % len(rows)]

    items: list[Case] = []

    # A01–A02：调价，现价（store_state 优先，否则标价）的 95%。
    for n in (1, 2):
        row = pick(n - 1)
        sku = str(row["sku_id"])
        price = _current_price(row, state)
        params = {"new_price": round(price * 0.95, 2)}
        items.append(
            _normal_case(
                f"A{n:02d}",
                type="adjust_price",
                params=params,
                sku_id=sku,
                label="调价 -5%",
                field="price",
            )
        )

    # A03–A04：调出价 1.20。选出价基准不会触发 +50% 护栏的 SKU。
    bid_candidates = [
        row
        for row in rows
        if (current := _as_float(state.get(str(row["sku_id"]), {}).get("bid"))) is None
        or current * 1.5 >= 1.2
    ]
    if len(bid_candidates) < 2:
        bid_candidates = rows
    for n, row in enumerate(bid_candidates[:2], start=3):
        sku = str(row["sku_id"])
        current_bid = _as_float(state.get(sku, {}).get("bid"))
        params = {"bid": _bid_value(current_bid)}
        items.append(
            _normal_case(
                f"A{n:02d}",
                type="adjust_bid",
                params=params,
                sku_id=sku,
                label="调出价 1.20",
                field="bid",
            )
        )

    # A05–A06：换素材。
    for n in (5, 6):
        row = pick(n - 1)
        sku = str(row["sku_id"])
        params = {
            "creative_id": f"{sku}-qa-{n}",
            "reason": "T7 评测：替换主图素材",
        }
        items.append(
            _normal_case(
                f"A{n:02d}",
                type="swap_creative",
                params=params,
                sku_id=sku,
                label="换主图",
                field="creative",
            )
        )

    # A07–A08：发券，面额按售价的 5% / 8% 推导，封顶 50 元、保底 5 元。
    for n in (7, 8):
        row = pick(n - 1)
        sku = str(row["sku_id"])
        price = _current_price(row, state)
        rate = 0.05 if n == 7 else 0.08
        amount = round(min(50.0, max(5.0, price * rate)), 2)
        params = {
            "amount": amount,
            "threshold": round(price * 1.2, 2),
            "quota": 20,
            "budget": round(amount * 20, 2),
        }
        items.append(
            _normal_case(
                f"A{n:02d}",
                type="issue_coupon",
                params=params,
                sku_id=sku,
                label="发券",
                field="coupon",
            )
        )

    # A09–A10：补货预警（risk 0，可直接执行）。
    for n in (9, 10):
        row = pick(n - 1)
        sku = str(row["sku_id"])
        params = {"days_of_stock": 3.0, "supplier": str(row["supplier"])}
        items.append(
            _normal_case(
                f"A{n:02d}",
                type="restock_alert",
                params=params,
                sku_id=sku,
                label="补货预警",
                field="stock",
            )
        )

    # A11：价格 +50%（超过 30% 硬上限）。用标价做基准，run 清理 store_state 后
    # 基准仍然是同一个值，避免「状态价远低于标价」时涨幅掉到 30% 以内。
    row = pick(0)
    sku = str(row["sku_id"])
    price = _as_float(row["price_tag"]) or _current_price(row, state)
    items.append(
        _guardrail_case(
            "A11",
            type="adjust_price",
            params={"new_price": round(price * 1.5, 2)},
            sku_id=sku,
            label="价格 +50%",
            contains="涨幅",
        )
    )

    # A12：出价 +200%（run 会先给该 SKU 种一个 1.00 的基准出价）。
    row = pick(1)
    sku = str(row["sku_id"])
    items.append(
        _guardrail_case(
            "A12",
            type="adjust_bid",
            params={"bid": 3.0},
            sku_id=sku,
            label="出价 +200%",
            contains="出价涨幅",
        )
    )

    # A13：券面额 500（超过 100 元上限）。
    row = pick(2)
    sku = str(row["sku_id"])
    items.append(
        _guardrail_case(
            "A13",
            type="issue_coupon",
            params={"amount": 500.0},
            sku_id=sku,
            label="券金额 500",
            contains="券面额",
        )
    )

    # A14：缺必填参数（换素材没有 creative_id）。
    row = pick(3)
    sku = str(row["sku_id"])
    items.append(
        _guardrail_case(
            "A14",
            type="swap_creative",
            params={},
            sku_id=sku,
            label="缺必填参数",
            contains="缺少必填参数",
        )
    )

    # A15：未知动作类型。
    row = pick(4)
    sku = str(row["sku_id"])
    items.append(
        _guardrail_case(
            "A15",
            type="launch_rocket",
            params={},
            sku_id=sku,
            label="未知动作类型",
            contains="未知动作类型",
        )
    )

    return items


# ---------------------------------------------------------------------------
# 临时库与状态清理
# ---------------------------------------------------------------------------


def _copy_database(src: Path, dst: Path) -> None:
    """复制主库；WAL 模式连 ``-wal`` 一起复制后 checkpoint，避免丢最后一次提交。"""
    if not src.is_file():
        raise FileNotFoundError(f"数据库不存在：{src}")
    shutil.copy2(src, dst)
    wal = Path(str(src) + "-wal")
    if wal.is_file():
        shutil.copy2(wal, Path(str(dst) + "-wal"))
    # 源库的 -shm 与当前进程绑定，复制过来反而可能过期；让 SQLite 用 -wal 重建。
    stale_shm = Path(str(dst) + "-shm")
    if stale_shm.exists():
        stale_shm.unlink()
    conn = sqlite3.connect(str(dst))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.Error:
        # 非 WAL 库或 checkpoint 被占用时，复制出来的主库+WAL 仍可用。
        pass
    finally:
        conn.close()


def _read_state(db_path: Path, store_id: str, sku_id: str, field: str) -> str | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM store_state WHERE store_id=? AND sku_id=? AND field=?",
            (store_id, sku_id, field),
        ).fetchone()
        return None if row is None else str(row["value"])
    finally:
        conn.close()


def _write_state(db_path: Path, store_id: str, sku_id: str, field: str, value: str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO store_state (store_id, sku_id, field, value, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(store_id, sku_id, field) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (store_id, sku_id, field, value),
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup(db_path: Path, sku_id: str) -> None:
    """清掉该 SKU 的动作与状态行（临时库内），避免用例之间互相污染/幂等命中。"""
    conn = _connect(db_path)
    try:
        action_ids = [
            str(row["action_id"])
            for row in conn.execute("SELECT action_id FROM action_log WHERE sku_id=?", (sku_id,))
        ]
        conn.execute("DELETE FROM store_state WHERE sku_id=?", (sku_id,))
        conn.execute("DELETE FROM action_log WHERE sku_id=?", (sku_id,))
        if action_ids:
            marks = ",".join("?" for _ in action_ids)
            conn.execute(f"DELETE FROM audit_log WHERE action_id IN ({marks})", action_ids)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 单例执行
# ---------------------------------------------------------------------------


def _run_normal(db_path: Path, case: Case) -> CaseResult:
    payload = case.payload
    sku = str(payload["sku_id"])
    field = str(payload["expect"]["field"])
    expected = str(payload["expect"]["next_value"])
    detail: dict[str, Any] = {
        "kind": "create",
        "type": payload["type"],
        "params": payload["params"],
        "sku_id": sku,
        "field": field,
        "expected": expected,
    }
    ok = False
    try:
        _cleanup(db_path, sku)
        before = _read_state(db_path, STORE_ID, sku, field)
        detail["state_before"] = before

        action = create_action(
            db_path,
            type=payload["type"],
            params=payload["params"],
            sku_id=sku,
            store_id=STORE_ID,
            trace_id=f"eval-{case.case_id}",
            reason="T7 动作评测",
            actor="qa",
        )
        detail["created"] = True
        detail["action_id"] = action["action_id"]
        detail["risk_level"] = int(action["risk_level"])
        detail["duplicate"] = bool(action.get("duplicate"))
        if detail["risk_level"] > 0:
            approved = approve_action(
                db_path,
                action["action_id"],
                approver="qa",
                role="manager",
                note="T7 评测审批",
            )
            detail["approved"] = approved["status"] == "approved"
        else:
            detail["approved"] = None

        executed = execute_action(db_path, action["action_id"], actor="qa")
        detail["executed"] = executed["status"] == "executed"
        got = _read_state(db_path, STORE_ID, sku, field)
        detail["state_after"] = got
        detail["state_matches"] = got == expected

        rolled = rollback_action(
            db_path, action["action_id"], actor="qa", reason="T7 评测回滚"
        )
        detail["rolled_back"] = rolled["status"] == "rolled_back"
        restored = _read_state(db_path, STORE_ID, sku, field)
        detail["state_restored"] = restored
        detail["restored"] = restored == before
        detail["revert"] = rolled.get("revert")

        ok = bool(
            detail["state_matches"]
            and detail["restored"]
            and detail["executed"]
            and detail["rolled_back"]
            and not detail["duplicate"]
        )
    except Exception as exc:  # noqa: BLE001 - 单例失败写进 detail，不拖垮整套
        detail["error"] = f"{type(exc).__name__}: {exc}"
        ok = False
    finally:
        try:
            _cleanup(db_path, sku)
        except Exception as exc:  # noqa: BLE001
            detail["cleanup_error"] = f"{type(exc).__name__}: {exc}"

    detail["ok"] = ok
    return CaseResult(case.case_id, ok, 1.0 if ok else 0.0, detail)


def _run_guardrail(db_path: Path, case: Case) -> CaseResult:
    payload = case.payload
    sku = str(payload["sku_id"])
    detail: dict[str, Any] = {
        "kind": "guardrail",
        "type": payload["type"],
        "params": payload["params"],
        "sku_id": sku,
        "expected_error": payload["expect"].get("contains", ""),
    }
    rejected = False
    message = ""
    try:
        _cleanup(db_path, sku)
        if payload["type"] == "adjust_bid":
            # 真实库没有历史出价；没有基准时 +50% 护栏会跳过，必须种一个基准。
            _write_state(db_path, STORE_ID, sku, "bid", "1.000")
        action = create_action(
            db_path,
            type=payload["type"],
            params=payload["params"],
            sku_id=sku,
            store_id=STORE_ID,
            trace_id=f"eval-{case.case_id}",
            reason="T7 护栏评测",
            actor="qa",
        )
        detail["unexpected_action"] = action.get("action_id")
    except ActionError as exc:
        rejected = True
        message = str(exc)
    except Exception as exc:  # noqa: BLE001 - 非 ActionError 也算失败，但保留类型
        detail["unexpected_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            _cleanup(db_path, sku)
        except Exception as exc:  # noqa: BLE001
            detail["cleanup_error"] = f"{type(exc).__name__}: {exc}"

    detail["rejected"] = rejected
    detail["message"] = message
    contains = str(payload["expect"].get("contains") or "")
    ok = bool(rejected and message.strip() and (not contains or contains in message))
    detail["ok"] = ok
    return CaseResult(case.case_id, ok, 1.0 if ok else 0.0, detail)


def _run_case(db_path: Path, case: Case) -> CaseResult:
    if case.payload.get("kind") == "guardrail":
        return _run_guardrail(db_path, case)
    return _run_normal(db_path, case)


# ---------------------------------------------------------------------------
# 套件入口
# ---------------------------------------------------------------------------


def _empty_meta() -> dict[str, Any]:
    return {"created": 0, "rejected": 0, "risk_counts": {}, "avg_ms": 0.0, "total": 0}


def _summarize(results: list[CaseResult], elapsed_ms: float) -> dict[str, Any]:
    created = sum(1 for r in results if r.detail.get("created"))
    rejected = sum(1 for r in results if r.detail.get("rejected"))
    risks = Counter(
        int(r.detail["risk_level"])
        for r in results
        if r.detail.get("created") and r.detail.get("risk_level") is not None
    )
    return {
        "created": created,
        "rejected": rejected,
        "risk_counts": dict(sorted(risks.items())),
        "avg_ms": round(elapsed_ms / len(results), 3) if results else 0.0,
        "total": len(results),
    }


def run(db_path: str | Path, *, limit: int | None = None) -> SuiteResult:
    started = time.perf_counter()
    db = Path(db_path)
    try:
        all_cases = cases(db)
    except Exception as exc:  # noqa: BLE001 - 缺库/缺表统一转成 SuiteResult.error
        elapsed = (time.perf_counter() - started) * 1000.0
        return SuiteResult(
            SUITE,
            [],
            elapsed_ms=elapsed,
            meta=_empty_meta(),
            error=f"无法生成动作用例：{type(exc).__name__}: {exc}",
        )

    selected = all_cases[: max(0, int(limit))] if limit is not None else all_cases
    if not selected:
        elapsed = (time.perf_counter() - started) * 1000.0
        return SuiteResult(
            SUITE,
            [],
            elapsed_ms=elapsed,
            meta=_empty_meta(),
            error="没有可执行的动作用例" if not all_cases else None,
        )

    tmp_dir = _TMP_ROOT / uuid.uuid4().hex
    results: list[CaseResult] = []
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_db = tmp_dir / "store.db"
        _copy_database(db, tmp_db)
        for case in selected:
            results.append(_run_case(tmp_db, case))
    except Exception as exc:  # noqa: BLE001 - 复制失败等基础设施问题
        elapsed = (time.perf_counter() - started) * 1000.0
        return SuiteResult(
            SUITE,
            results,
            elapsed_ms=elapsed,
            meta=_summarize(results, elapsed),
            error=f"动作套件运行失败：{type(exc).__name__}: {exc}",
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    elapsed = (time.perf_counter() - started) * 1000.0
    return SuiteResult(SUITE, results, elapsed_ms=elapsed, meta=_summarize(results, elapsed))
