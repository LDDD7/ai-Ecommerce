"""把规范行写进现有的事实/维度表：幂等、可审计、可撤销。

幂等靠主键：订单主键是 `order_id`，流量是 `(dt, sku_id, channel_id)`，
所以同一份文件重复导入不会把 GMV 翻倍——这是 toB 场景最基本的要求。

但「不报错」不等于「没变化」：同一订单第二次导出时金额可能不同（部分退款后重算），
所以入库前先算一次差异（新增 / 变更 / 无变化），变更行必须显式确认；
同时把这批行的「改前值」记进 `ingest_undo`，导错了可以按批次撤销。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

INGEST_RUN_DDL = """
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id      TEXT PRIMARY KEY,
    dataset     TEXT NOT NULL,
    filename    TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    rows_total  INTEGER NOT NULL,
    rows_written INTEGER NOT NULL,
    inserted    INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    changed     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active',
    backup_path TEXT NOT NULL DEFAULT '',
    verdict     TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
)
"""

INGEST_UNDO_DDL = """
CREATE TABLE IF NOT EXISTS ingest_undo (
    run_id     TEXT PRIMARY KEY,
    dataset    TEXT NOT NULL,
    payload    BLOB NOT NULL,
    created_at TEXT NOT NULL
)
"""

# 老库升级：ALTER TABLE 一次一列（SQLite 不支持 IF NOT EXISTS 加列）
_RUN_COLUMNS = {
    "changed": "INTEGER NOT NULL DEFAULT 0",
    "unchanged": "INTEGER NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL DEFAULT 'active'",
    "backup_path": "TEXT NOT NULL DEFAULT ''",
}

CHANNEL_DEFAULT = "CH_OTHER"
SEGMENT_DEFAULT = "SEG_OFFICE"
MONEY_TOLERANCE = 0.01
SAMPLE_LIMIT = 10
UNDO_ROW_LIMIT = 20_000


class ChangesNeedConfirm(ValueError):
    """有行会被覆盖，但调用方没有确认。"""

    def __init__(self, diff: dict[str, Any]):
        self.diff = diff
        super().__init__(f"{diff['updated']} 行会覆盖已有数据，需要确认后再提交")


@dataclass(frozen=True)
class _Spec:
    table: str
    keys: tuple[str, ...]
    fields: tuple[str, ...]
    money: tuple[str, ...] = ()


_SPECS: dict[str, _Spec] = {
    "orders": _Spec(
        "fact_orders",
        ("order_id",),
        ("dt", "sku_id", "channel_id", "segment_id", "qty", "unit_price",
         "pay_amount", "discount", "status"),
        money=("unit_price", "pay_amount", "discount"),
    ),
    "traffic": _Spec(
        "fact_traffic_daily",
        ("dt", "sku_id", "channel_id"),
        ("impressions", "clicks", "detail_uv", "add_cart_uv", "pay_uv",
         "paid_orders", "gmv"),
        money=("gmv",),
    ),
    "refunds": _Spec(
        "fact_refunds",
        ("refund_id",),
        ("order_id", "dt", "sku_id", "reason_code", "amount", "status"),
        money=("amount",),
    ),
    "products": _Spec(
        "dim_sku",
        ("sku_id",),
        ("title", "category_l1", "category_l2", "price_tag", "cost",
         "launch_date", "supplier", "style", "fabric", "color", "price_band"),
        money=("price_tag", "cost"),
    ),
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(INGEST_RUN_DDL)
    conn.execute(INGEST_UNDO_DDL)
    have = {str(row[1]) for row in conn.execute("PRAGMA table_info(ingest_run)")}
    for name, decl in _RUN_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE ingest_run ADD COLUMN {name} {decl}")
    conn.commit()


def _resolve_channel(conn: sqlite3.Connection, raw: str | None) -> str:
    if not raw:
        return CHANNEL_DEFAULT
    wanted = str(raw).strip().lower()
    for channel_id, name, kind in conn.execute("SELECT channel_id, name, type FROM dim_channel"):
        if wanted in {str(name).lower(), str(kind).lower(), str(channel_id).lower()}:
            return str(channel_id)
    for channel_id, name, _kind in conn.execute("SELECT channel_id, name, type FROM dim_channel"):
        if wanted and (wanted in str(name).lower() or str(name).lower() in wanted):
            return str(channel_id)
    conn.execute(
        "INSERT OR IGNORE INTO dim_channel(channel_id, name, type) VALUES (?, ?, ?)",
        (CHANNEL_DEFAULT, "其他渠道", "other"),
    )
    return CHANNEL_DEFAULT


def _price_band(price: float) -> str:
    if price < 100:
        return "0-99"
    if price < 200:
        return "100-199"
    if price < 500:
        return "200-499"
    return "500+"


def refund_id_of(row: dict[str, Any]) -> str:
    """没有售后单号时生成稳定 id：同一订单、同一天、同一金额，重导不会再插一条。

    代价是「同单同天同金额同原因」的两笔退款会被合并成一条——比行序变化就翻倍要好。
    """
    given = str(row.get("refund_id") or "").strip()
    if given:
        return given
    basis = "|".join(
        str(row.get(key) or "") for key in ("order_id", "dt", "amount", "reason")
    )
    return "RF" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12].upper()


def _ensure_sku(conn: sqlite3.Connection, sku_id: str) -> None:
    """订单/流量里出现的商品，补一行最小维度，避免事实表 JOIN 丢行。"""
    exists = conn.execute("SELECT 1 FROM dim_sku WHERE sku_id = ?", (sku_id,)).fetchone()
    if exists:
        return
    conn.execute(
        """
        INSERT INTO dim_sku(sku_id, title, category_l1, category_l2, price_tag, cost,
                            launch_date, image_version, supplier, style, fabric, color, price_band)
        VALUES (?, ?, '未分类', '未分类', 0, 0, '2026-01-01', 1, '外部导入', '未标注', '未标注', '未标注', '0-99')
        """,
        (sku_id, sku_id),
    )


def _resolve_segment(conn: sqlite3.Connection, raw: str | None) -> str:
    rows = list(conn.execute("SELECT segment_id, name FROM dim_segment"))
    if raw:
        wanted = str(raw).strip().lower()
        for segment_id, name in rows:
            if wanted in {str(name).lower(), str(segment_id).lower()}:
                return str(segment_id)
    return str(rows[0][0]) if rows else SEGMENT_DEFAULT


def _ensure_dates(conn: sqlite3.Connection, dates: set[str]) -> None:
    for value in sorted(dates):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            continue
        iso_year, iso_week, _ = day.isocalendar()
        conn.execute(
            """
            INSERT OR IGNORE INTO dim_date(dt, dow, week, month, is_promo, promo_name, season)
            VALUES (?, ?, ?, ?, 0, NULL, ?)
            """,
            (
                value,
                day.isoweekday(),
                f"{iso_year}-W{iso_week:02d}",
                day.strftime("%Y-%m"),
                "旺季" if day.month in (9, 10, 11, 12, 1) else "淡季",
            ),
        )


def _dates_of(rows: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        value = row.get("dt")
        if value:
            out.add(str(value)[:10])
    return out


# ---------- 规范行 → 待写值 ----------

def _order_values(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    sku_id = str(row["sku_id"])
    qty = int(row.get("qty") or 1)
    pay = float(row.get("pay_amount") or 0.0)
    price = float(row.get("unit_price") or (pay / qty if qty else pay))
    return {
        "order_id": str(row["order_id"]),
        "dt": str(row["dt"])[:10],
        "sku_id": sku_id,
        "channel_id": _resolve_channel(conn, row.get("channel")),
        "segment_id": _resolve_segment(conn, row.get("segment")),
        "qty": qty,
        "unit_price": round(price, 2),
        "pay_amount": round(pay, 2),
        "discount": round(float(row.get("discount") or 0.0), 2),
        "status": str(row.get("status") or "finished"),
    }


def _traffic_values(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "dt": str(row["dt"])[:10],
        "sku_id": str(row["sku_id"]),
        "channel_id": _resolve_channel(conn, row.get("channel")),
        "impressions": int(row.get("impressions") or 0),
        "clicks": int(row.get("clicks") or 0),
        "detail_uv": int(row.get("detail_uv") or 0),
        "add_cart_uv": int(row.get("add_cart_uv") or 0),
        "pay_uv": int(row.get("pay_uv") or 0),
        "paid_orders": int(row.get("paid_orders") or 0),
        "gmv": round(float(row.get("gmv") or 0.0), 2),
    }


def _refund_values(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_id": refund_id_of(row),
        "order_id": str(row.get("order_id") or ""),
        "dt": str(row.get("dt") or "")[:10],
        "sku_id": str(row.get("sku_id") or ""),
        "reason_code": str(row.get("reason") or "unknown"),
        "amount": round(float(row.get("amount") or 0.0), 2),
        "status": str(row.get("status") or "finished"),
    }


def _product_values(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """商品表按「文件里给了什么就更新什么」，返回（插入值, 仅更新提供的列）。"""
    sku_id = str(row["sku_id"])
    provided: dict[str, Any] = {}
    for key in ("title", "category_l1", "category_l2", "supplier", "style", "fabric", "color"):
        value = row.get(key)
        if value not in (None, ""):
            provided[key] = str(value)
    for key in ("price_tag", "cost"):
        if row.get(key) not in (None, ""):
            provided[key] = round(float(row[key]), 2)
    if row.get("launch_date"):
        provided["launch_date"] = str(row["launch_date"])[:10]
    if "price_tag" in provided:
        provided["price_band"] = _price_band(float(provided["price_tag"]))
    insert = {
        "sku_id": sku_id,
        "title": sku_id,
        "category_l1": "未分类",
        "category_l2": "未分类",
        "price_tag": 0.0,
        "cost": 0.0,
        "launch_date": "2026-01-01",
        "image_version": 1,
        "supplier": "外部导入",
        "style": "未标注",
        "fabric": "未标注",
        "color": "未标注",
        "price_band": "0-99",
    }
    insert.update(provided)
    return insert, provided


def _values_for(dataset: str, conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    if dataset == "orders":
        return _order_values(conn, row)
    if dataset == "traffic":
        return _traffic_values(conn, row)
    if dataset == "refunds":
        return _refund_values(conn, row)
    if dataset == "products":
        return _product_values(row)[0]
    raise ValueError(f"不支持写入的数据集：{dataset}")


def _read_existing(conn: sqlite3.Connection, dataset: str, values: dict[str, Any]) -> dict[str, Any] | None:
    spec = _SPECS[dataset]
    where = " AND ".join(f"{key} = ?" for key in spec.keys)
    params = tuple(values[key] for key in spec.keys)
    row = conn.execute(f"SELECT {', '.join(spec.fields)} FROM {spec.table} WHERE {where}", params).fetchone()
    if row is None:
        return None
    return dict(zip(spec.fields, row))


def _is_equal(field: str, before: Any, after: Any, spec: _Spec) -> bool:
    if field in spec.money:
        try:
            return abs(float(before or 0) - float(after or 0)) <= MONEY_TOLERANCE
        except (TypeError, ValueError):
            return before == after
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return float(before) == float(after)
    return str(before if before is not None else "") == str(after if after is not None else "")


def _diff(conn: sqlite3.Connection, dataset: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """读出这批键的现有值，和「即将写入的值」逐字段比。"""
    spec = _SPECS[dataset]
    ops: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    new = updated = unchanged = 0
    for row in rows:
        values = _values_for(dataset, conn, row)
        if dataset == "products":
            _insert, provided = _product_values(row)
            compare = provided
        else:
            compare = {key: values[key] for key in spec.fields}
        before = _read_existing(conn, dataset, values)
        if before is None:
            new += 1
            ops.append({"dataset": dataset, "keys": {k: values[k] for k in spec.keys}, "before": None})
            continue
        fields = tuple(compare) if dataset == "products" else spec.fields
        changes = [
            {"field": field, "before": before.get(field), "after": compare[field]}
            for field in fields
            if field in compare and not _is_equal(field, before.get(field), compare[field], spec)
        ]
        if not changes:
            unchanged += 1
            continue
        updated += 1
        ops.append({
            "dataset": dataset,
            "keys": {k: values[k] for k in spec.keys},
            "before": {k: before.get(k) for k in spec.fields},
        })
        if len(samples) < SAMPLE_LIMIT:
            samples.append({
                "key": " / ".join(str(values[k]) for k in spec.keys),
                "changes": changes[:5],
            })
    return {
        "dataset": dataset,
        "rows": len(rows),
        "inserted": new,
        "updated": updated,
        "unchanged": unchanged,
        "changed_count": updated,
        "samples": samples,
        "undo_ops": ops,
    }


def peek_diff(db_path: str, dataset: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """只读地算一次差异，给「提交前确认」用。"""
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        diff = _diff(conn, dataset, rows)
    finally:
        conn.close()
    diff.pop("undo_ops", None)
    return diff


# ---------- 写入 ----------

def _write_values(conn: sqlite3.Connection, dataset: str, values: dict[str, Any]) -> None:
    spec = _SPECS[dataset]
    columns = list(values)
    update_columns = [column for column in columns if column not in spec.keys]
    assignments = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    conn.execute(
        f"""
        INSERT INTO {spec.table}({', '.join(columns)})
        VALUES ({', '.join('?' for _ in columns)})
        ON CONFLICT({', '.join(spec.keys)}) DO UPDATE SET {assignments}
        """,
        tuple(values[column] for column in columns),
    )


def _write_rows(conn: sqlite3.Connection, dataset: str, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if dataset in ("orders", "traffic"):
            _ensure_sku(conn, str(row["sku_id"]))
        if dataset == "products":
            values, _provided = _product_values(row)
            _write_values(conn, dataset, values)
            continue
        _write_values(conn, dataset, _values_for(dataset, conn, row))


def backup(db_path: str, *, root: Path) -> Path:
    """入库前先整库备份到 .tmp（不写 data/，那个目录的 ACL 只允许追加式写入）。"""
    target_dir = root / ".tmp" / "ingest" / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"store-{stamp}.db"
    source = sqlite3.connect(db_path)
    try:
        with sqlite3.connect(target) as dest:
            source.backup(dest)
    finally:
        source.close()
    return target


def commit(
    db_path: str,
    dataset: str,
    rows: list[dict[str, Any]],
    *,
    filename: str,
    file_sha256: str,
    report: dict[str, Any],
    mapping: dict[str, str],
    root: Path,
    confirm_changes: bool = False,
) -> dict[str, Any]:
    if dataset not in _SPECS:
        raise ValueError(f"不支持写入的数据集：{dataset}")

    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        diff = _diff(conn, dataset, rows)
        if diff["updated"] and not confirm_changes:
            raise ChangesNeedConfirm({k: v for k, v in diff.items() if k != "undo_ops"})

        backup_path = backup(db_path, root=root)
        _ensure_dates(conn, _dates_of(rows))
        with conn:  # 一个事务：要么全进，要么全不进
            _write_rows(conn, dataset, rows)
            run_id = "run_" + hashlib.sha1(
                f"{dataset}|{file_sha256}|{datetime.now().isoformat()}".encode()
            ).hexdigest()[:16]
            conn.execute(
                """
                INSERT INTO ingest_run(run_id, dataset, filename, file_sha256, rows_total,
                                       rows_written, inserted, updated, changed, unchanged,
                                       status, backup_path, verdict, mapping_json,
                                       report_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    dataset,
                    filename,
                    file_sha256,
                    int(report.get("rows_total") or 0),
                    len(rows),
                    diff["inserted"],
                    diff["updated"],
                    diff["updated"],
                    diff["unchanged"],
                    str(backup_path),
                    str(report.get("verdict") or "pass"),
                    _json(mapping),
                    _json(report),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            ops = diff["undo_ops"]
            if len(ops) <= UNDO_ROW_LIMIT:
                conn.execute(
                    "INSERT OR REPLACE INTO ingest_undo(run_id, dataset, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        run_id,
                        dataset,
                        zlib.compress(_json(ops).encode("utf-8")),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
    finally:
        conn.close()

    return {
        "run_id": run_id,
        "dataset": dataset,
        "written": len(rows),
        "inserted": diff["inserted"],
        "updated": diff["updated"],
        "unchanged": diff["unchanged"],
        "changed_count": diff["updated"],
        "samples": diff["samples"],
        "undoable": len(diff["undo_ops"]) <= UNDO_ROW_LIMIT,
        "backup": str(backup_path),
        "promo": _refresh_promo(db_path, rows),
    }


def _refresh_promo(db_path: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """导入后按促销日历重算本次涉及的日期（日历为空则不动）。"""
    days = sorted({str(row.get("dt")) for row in rows if row.get("dt")})
    if not days:
        return None
    try:
        from app import promo

        promo.ensure_schema(db_path)
        sync = promo.rebuild_flags(db_path, start=days[0], end=days[-1], force=False)
        return sync if sync.get("rebuilt") else {"rebuilt": False, "reason": sync.get("reason")}
    except Exception as exc:  # 促销标记失败不能拖垮导入
        return {"rebuilt": False, "reason": f"{type(exc).__name__}: {exc}"}


def undo_run(db_path: str, run_id: str) -> dict[str, Any]:
    """按批次撤销：改动过的行写回改前值，新增的行删掉。"""
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT dataset, status FROM ingest_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"找不到批次 {run_id}")
        dataset, status = str(row[0]), str(row[1])
        if status == "undone":
            raise ValueError("这一批已经撤销过了")
        payload = conn.execute(
            "SELECT payload FROM ingest_undo WHERE run_id = ?", (run_id,)
        ).fetchone()
        if payload is None:
            raise ValueError("这一批没有留下撤销记录（批次过大或来自旧版本），请用整库备份恢复")

        ops = json.loads(zlib.decompress(payload[0]).decode("utf-8"))
        spec = _SPECS[dataset]
        deleted = restored = 0
        with conn:
            for op in reversed(ops):  # 倒序撤销，避免同键多次改动时互相覆盖
                keys = op["keys"]
                where = " AND ".join(f"{key} = ?" for key in spec.keys)
                params = tuple(keys[key] for key in spec.keys)
                if op.get("before") is None:
                    if dataset == "products" and _sku_referenced(conn, keys["sku_id"]):
                        continue
                    conn.execute(f"DELETE FROM {spec.table} WHERE {where}", params)
                    deleted += 1
                else:
                    before = op["before"]
                    sets = ", ".join(f"{key} = ?" for key in before)
                    conn.execute(
                        f"UPDATE {spec.table} SET {sets} WHERE {where}",
                        tuple(before[key] for key in before) + params,
                    )
                    restored += 1
            conn.execute("UPDATE ingest_run SET status = 'undone' WHERE run_id = ?", (run_id,))
    finally:
        conn.close()
    return {"run_id": run_id, "dataset": dataset, "deleted": deleted, "restored": restored}


def _sku_referenced(conn: sqlite3.Connection, sku_id: str) -> bool:
    for table in ("fact_orders", "fact_traffic_daily", "fact_refunds"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE sku_id = ? LIMIT 1", (sku_id,)).fetchone():
            return True
    return False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def recent_runs(db_path: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT run_id, dataset, filename, rows_total, rows_written, inserted, updated,
                   changed, unchanged, status, verdict, created_at
            FROM ingest_run ORDER BY created_at DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    keys = ("run_id", "dataset", "filename", "rows_total", "rows_written", "inserted",
            "updated", "changed", "unchanged", "status", "verdict", "created_at")
    return [dict(zip(keys, row)) for row in rows]
