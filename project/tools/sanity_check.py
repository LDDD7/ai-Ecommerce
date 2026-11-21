"""数据自检：确认合成数据「看起来像真的」且注入的根因确实体现在指标里。

    python tools/sanity_check.py --db data/store.db
    python tools/sanity_check.py --db data/store_tiny.db

检查项
1. 各表行数、日期覆盖
2. 大盘：GMV / 订单 / AOV / 退款率 / 转化率
3. 逐条 anomaly_gt：窗口内 vs 窗口前 28 天，受影响指标的变化幅度
4. 素材：图片数量与体积
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# code -> 用于验证的指标（与注入的 effects 对应）
CHECK_METRIC = {
    "R1": "cvr",
    "R2": "ctr",
    "R3": "paid_orders",
    "R4": "refund_rate",
    "R5": "roi",
    "R6": "neg_review_rate",
    "R7": "impressions",
    "R8": "impressions",
    "R9": "cvr",
    "R10": "refund_rate",
}


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def one(con, sql, args=()):
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None


def metric_value(con, metric: str, d0: str, d1: str, skus: list[str] | None) -> float | None:
    where = "dt BETWEEN ? AND ?"
    args: list = [d0, d1]
    if skus:
        where += f" AND sku_id IN ({','.join('?' * len(skus))})"
        args += skus
    ndays = (date.fromisoformat(d1) - date.fromisoformat(d0)).days + 1

    if metric == "impressions":
        return (one(con, f"SELECT SUM(impressions) FROM fact_traffic_daily WHERE {where}", args) or 0) / ndays
    if metric == "ctr":
        imp = one(con, f"SELECT SUM(impressions) FROM fact_traffic_daily WHERE {where}", args) or 0
        clk = one(con, f"SELECT SUM(clicks) FROM fact_traffic_daily WHERE {where}", args) or 0
        return clk / imp if imp else None
    if metric == "cvr":
        det = one(con, f"SELECT SUM(detail_uv) FROM fact_traffic_daily WHERE {where}", args) or 0
        pay = one(con, f"SELECT SUM(paid_orders) FROM fact_traffic_daily WHERE {where}", args) or 0
        return pay / det if det else None
    if metric == "paid_orders":
        return (one(con, f"SELECT SUM(paid_orders) FROM fact_traffic_daily WHERE {where}", args) or 0) / ndays
    if metric == "refund_rate":
        # 退款按「下单日」归属（join fact_orders），否则退款日会跨窗、稀释信号
        orders = one(con, f"SELECT COUNT(*) FROM fact_orders WHERE {where}", args) or 0
        rcond = f"o.dt BETWEEN ? AND ?"
        if skus:
            rcond += f" AND o.sku_id IN ({','.join('?' * len(skus))})"
        refunds = one(
            con,
            f"SELECT COUNT(*) FROM fact_refunds f JOIN fact_orders o ON o.order_id = f.order_id"
            f" WHERE {rcond}", args) or 0
        return refunds / orders if orders else None
    if metric == "roi":
        gmv = one(con, f"SELECT SUM(gmv) FROM fact_traffic_daily WHERE {where}", args) or 0
        spend = one(con, f"SELECT SUM(spend) FROM fact_ads WHERE {where}", args) or 0
        return gmv / spend if spend else None
    if metric == "neg_review_rate":
        total = one(con, f"SELECT COUNT(*) FROM fact_reviews WHERE {where}", args) or 0
        bad = one(con, f"SELECT COUNT(*) FROM fact_reviews WHERE {where} AND rating <= 2", args) or 0
        return bad / total if total else None
    raise ValueError(metric)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，¥ 等字符会炸
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/store.db")
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    print(f"== {args.db}  ({Path(args.db).stat().st_size / 1e6:.1f} MB) ==")
    tables = [r[0] for r in q(con, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        print(f"  {t:<24} {one(con, f'SELECT COUNT(*) FROM {t}'):>9,}")

    d0, d1 = q(con, "SELECT MIN(dt), MAX(dt) FROM dim_date")[0]
    print(f"\n日期覆盖: {d0} ~ {d1}")

    gmv = one(con, "SELECT SUM(gmv) FROM fact_traffic_daily")
    orders = one(con, "SELECT SUM(paid_orders) FROM fact_traffic_daily")
    buyers = one(con, "SELECT SUM(pay_uv) FROM fact_traffic_daily")
    refunds = one(con, "SELECT COUNT(*) FROM fact_refunds")
    imp = one(con, "SELECT SUM(impressions) FROM fact_traffic_daily")
    clk = one(con, "SELECT SUM(clicks) FROM fact_traffic_daily")
    print(f"GMV: ¥{gmv:,.0f}   订单: {orders:,}   买家: {buyers:,}   AOV: ¥{gmv / max(orders, 1):.1f}")
    print(f"曝光: {imp:,}   CTR: {clk / max(imp, 1):.2%}   退款率: {refunds / max(orders, 1):.2%}")

    print("\n== 注入根因验证（窗口内 vs 前 28 天，同 SKU 集合）==")
    rows = q(con, "SELECT * FROM anomaly_gt ORDER BY code")
    for r in rows:
        metric = CHECK_METRIC[r["code"]]
        skus = r["sku_id"].split(",") if r["sku_id"] else None
        d0w, d1w = r["dt_start"], r["dt_end"]
        base_end = date.fromisoformat(d0w) - timedelta(days=1)
        base_start = base_end - timedelta(days=27)
        v_win = metric_value(con, metric, d0w, d1w, skus)
        v_base = metric_value(con, metric, base_start.isoformat(), base_end.isoformat(), skus)
        delta = (v_win / v_base - 1) if (v_win and v_base) else None
        delta_s = "-" if delta is None else f"{delta:+.1%}"
        win_s = "-" if v_win is None else f"{v_win:.4f}"
        base_s = "-" if v_base is None else f"{v_base:.4f}"
        flag = ""
        if delta is not None:
            if metric in ("impressions", "paid_orders") and delta < -0.15:
                flag = "OK"
            elif metric in ("cvr", "ctr") and delta < -0.08:
                flag = "OK"
            elif metric in ("refund_rate", "neg_review_rate") and delta > 0.15:
                flag = "OK"
            elif metric == "roi" and delta < -0.10:
                flag = "OK"
            else:
                flag = "??"
        print(f"  {r['code']:<4}{r['name']:<10}{metric:<16}"
              f"win={win_s:<10} base={base_s:<10} Δ={delta_s:<9}{flag}")

    print("\n== 素材 ==")
    n_img = one(con, "SELECT COUNT(*) FROM image_feature")
    print(f"image_feature 行数: {n_img}")
    img_root = Path(args.db).resolve().parent / "images"
    files = list(img_root.glob("*.png")) if img_root.exists() else []
    size = sum(f.stat().st_size for f in files)
    print(f"PNG 文件: {len(files)}  合计 {size / 1e6:.1f} MB  目录 {img_root}")

    print("\n== 退款原因分布 ==")
    for r in q(con, "SELECT reason_code, COUNT(*) c FROM fact_refunds GROUP BY 1 ORDER BY c DESC"):
        print(f"  {r['reason_code']:<14}{r['c']:>7}")

    print("\n== Top 5 SKU（GMV）==")
    for r in q(con, """SELECT sku_id, SUM(gmv) g, SUM(paid_orders) o FROM fact_traffic_daily
                        GROUP BY sku_id ORDER BY g DESC LIMIT 5"""):
        print(f"  {r['sku_id']}  ¥{r['g']:>10,.0f}  {r['o']:>6} 单")

    con.close()
    print("\nsanity ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
