"""把「根因 → 退款原因」的结构信号补进已有数据（数据 v2）。

背景：生成器原先的退款原因与注入根因无关（`weighted_choice(rng, REFUND_REASONS)`），
导致 R4（尺码与描述不符）与 R10（物流时效恶化）在数据上完全不可分——两者都只表现为
「退款率上升」。真实电商区分它们靠的是原因码结构（尺码 vs 物流），所以这里按
`anomaly_gt` 的窗口把原因码结构补上：

- R4 窗口内的退款 → 80% 记 `SIZE_SMALL`（`return_rate` 随之上升）
- R10 窗口内的退款 → 80% 记 `LOGISTICS`（`return_rate` 相对下降）

只改 `fact_refunds.reason_code`，不动退款条数、金额、日期——所以 `refund_rate` 不变，
只有 `return_rate`（基于 `SIZE_SMALL` 的占比）会动。生成器里已同步加 `REASON_BIAS`，
以后重新 seed 会直接带这个结构。

用法：
    python -m tools.migrate_refund_reasons [--db data/store.db] [--dry-run]

注意：`project/data/` 目录有继承的 DENY ACE，SQLite 无法在其中新建 `-wal`/`-shm`，
所以这里走「复制到 .tmp → 改 → 原地覆盖」的路子（与修 store_id 时同一套办法）。
"""

from __future__ import annotations

import argparse
import random
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REASON_BIAS = {"R4": "SIZE_SMALL", "R10": "LOGISTICS"}
BIAS_RATE = 0.80


@dataclass(frozen=True)
class BiasWindow:
    code: str
    start: str
    end: str
    skus: frozenset[str]


def load_windows(con: sqlite3.Connection) -> list[BiasWindow]:
    rows = con.execute(
        "SELECT code, dt_start, dt_end, sku_id FROM anomaly_gt WHERE code IN ('R4', 'R10')"
    ).fetchall()
    windows = []
    for code, start, end, sku_ids in rows:
        if not sku_ids:
            # 全店级注入没有具体 SKU，用默认原因分布即可
            continue
        skus = frozenset(part.strip() for part in str(sku_ids).split(",") if part.strip())
        windows.append(BiasWindow(code=code, start=str(start), end=str(end), skus=skus))
    return windows


def target_reason(sku_id: str, order_dt: str, windows: list[BiasWindow]) -> str | None:
    for win in windows:
        if win.start <= order_dt <= win.end and sku_id in win.skus:
            return REASON_BIAS[win.code]
    return None


def migrate(db_path: Path, *, dry_run: bool = False) -> dict[str, int]:
    work = Path(".tmp") / "migrate_reasons"
    work.mkdir(parents=True, exist_ok=True)
    tmp_db = work / db_path.name
    shutil.copy2(db_path, tmp_db)
    for suffix in ("-wal", "-shm"):
        extra = work / (db_path.name + suffix)
        if extra.exists():
            extra.unlink()

    con = sqlite3.connect(tmp_db)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        windows = load_windows(con)
        rows = con.execute(
            """
            SELECT rf.refund_id, o.sku_id, o.dt, rf.reason_code
            FROM fact_refunds rf
            JOIN fact_orders o ON o.order_id = rf.order_id
            """
        ).fetchall()
        stats = {"total": len(rows), "r4": 0, "r10": 0, "changed": 0}
        updates: list[tuple[str, str]] = []
        for refund_id, sku_id, order_dt, current in rows:
            wanted = target_reason(str(sku_id), str(order_dt), windows)
            if wanted is None:
                continue
            stats["r4" if wanted == "SIZE_SMALL" else "r10"] += 1
            rng = random.Random(f"{refund_id}|reason-v2")
            if rng.random() >= BIAS_RATE or current == wanted:
                continue
            updates.append((wanted, refund_id))
        stats["changed"] = len(updates)
        if dry_run:
            return stats
        con.executemany("UPDATE fact_refunds SET reason_code = ? WHERE refund_id = ?", updates)
        con.commit()
        stats["return_rate"] = round(
            con.execute(
                "SELECT AVG(CASE WHEN reason_code = 'SIZE_SMALL' THEN 1.0 ELSE 0.0 END) FROM fact_refunds"
            ).fetchone()[0],
            4,
        )
        stats["logistics_rate"] = round(
            con.execute(
                "SELECT AVG(CASE WHEN reason_code = 'LOGISTICS' THEN 1.0 ELSE 0.0 END) FROM fact_refunds"
            ).fetchone()[0],
            4,
        )
    finally:
        con.close()

    # 原地覆盖（已存在文件不需要在目录里新建条目）
    with tmp_db.open("rb") as src, db_path.open("r+b") as dst:
        shutil.copyfileobj(src, dst)
        dst.truncate()
    for suffix in ("-wal", "-shm"):
        extra = work / (db_path.name + suffix)
        if extra.exists():
            extra.unlink()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把根因结构写入退款原因码（数据 v2）")
    parser.add_argument("--db", default="data/store.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        return 1
    stats = migrate(db_path, dry_run=args.dry_run)
    mode = "dry-run" if args.dry_run else "done"
    print(f"[{mode}] {db_path} {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
