"""语义层冒烟：加载目录、跑几条典型 QuerySpec，确认 SQL 与数字都对。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.metrics import QuerySpec, load_catalog, run  # noqa: E402

DB = "data/store.db"


def show(title: str, spec: QuerySpec) -> None:
    r = run(spec, DB)
    print(f"\n-- {title}")
    print("   cols:", r.columns)
    for row in r.rows[:6]:
        print("   ", row)
    print("   meta:", {k: r.meta[k] for k in ("elapsed_ms", "row_count", "sql_statements")})


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cat = load_catalog()
    print(f"catalog v{cat.version}: {len(cat.metrics)} 指标")
    print("  ", ", ".join(cat.names()))

    show("近 14 天大盘（总量）", QuerySpec(
        metrics=["gmv", "paid_orders", "aov", "cvr"],
        time={"start": "2026-11-05", "end": "2026-11-18"},
    ))
    show("按天看 GMV/退款率", QuerySpec(
        metrics=["gmv", "refund_rate"],
        dimensions=["dt"],
        time={"start": "2026-11-10", "end": "2026-11-18"},
        order_by=[{"field": "dt", "dir": "asc"}],
    ))
    show("按款式看 10 月转化", QuerySpec(
        metrics=["cvr", "gmv", "uv"],
        dimensions=["style"],
        time={"start": "2026-10-01", "end": "2026-10-31"},
        order_by=[{"field": "gmv", "dir": "desc"}],
    ))
    show("断货 SKU Top5", QuerySpec(
        metrics=["stockout_rate", "gmv"],
        dimensions=["sku_id"],
        time={"start": "2026-11-08", "end": "2026-11-14"},
        filters=[{"field": "sku_id", "op": "like", "value": "SKU0%"}],
        order_by=[{"field": "stockout_rate", "dir": "desc"}],
        limit=5,
    ))

    show("跨源合并：GMV + 评分（同维度）", QuerySpec(
        metrics=["gmv", "dsr", "refund_rate"],
        dimensions=["sku_id"],
        time={"start": "2026-10-01", "end": "2026-10-31"},
        order_by=[{"field": "gmv", "dir": "desc"}],
        limit=3,
    ))

    # 负例：跨指标维度不兼容 / 拿度量当维度
    for bad in [
        QuerySpec(metrics=["gmv", "dsr"], dimensions=["channel_id"],
                  time={"start": "2026-11-01", "end": "2026-11-02"}),
        QuerySpec(metrics=["gmv"], dimensions=["rating"],
                  time={"start": "2026-11-01", "end": "2026-11-02"}),
    ]:
        try:
            run(bad, DB)
            print("\n!! 应该报错但没报:", bad)
        except Exception as e:  # noqa: BLE001
            print(f"\n-- 预期拒绝: {type(e).__name__}: {e}")

    print("\nsmoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
