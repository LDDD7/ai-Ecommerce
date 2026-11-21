"""体检台概览：KPI、健康分、异常卡、趋势。

W1 阶段这里是**规则版**扫描（阈值 + 指标变化），目的是先把"能看见"跑通；
W2 会换成真正的归因引擎（恒等式分解 + 证据链 + 贡献占比），届时本模块只保留
KPI/趋势这类确定性聚合，异常卡由 `app.insights.anomaly` 提供。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.metrics import QuerySpec, run
from app.metrics.spec import MetricError

WINDOW_DAYS = 7
TREND_DAYS = 30

# 卡片触发阈值
DELTA_TRIGGER = 0.15          # 相对变化 ≥15% 才出卡
VOLUME_FLOOR_ORDERS = 5       # 单维度切片至少 5 单，避免小样本噪声
MAX_CARDS = 6

# 指标 → 展示口径
KPI_METRICS = ["gmv", "paid_orders", "aov", "cvr", "refund_rate", "roi"]
SCAN_METRICS = ["impressions", "ctr", "cvr", "paid_orders", "gmv", "refund_rate"]

# W1 规则映射：命中哪个信号 → 给哪个疑似根因（W2 由归因引擎替换）
RULES: list[tuple[str, str, str]] = [
    ("ctr", "R2", "主图素材衰减"),
    ("impressions", "R8", "竞争加剧 / 流量下滑"),
    ("paid_orders", "R3", "热销断货"),
    ("cvr", "R1", "竞品降价战 / 转化下滑"),
    ("refund_rate", "R4", "尺码与描述不符"),
]

DIM_LABELS = {"style": "款式", "channel_id": "渠道", "sku_id": "商品"}


def _date_bounds(db_path: str | Path) -> tuple[date, date]:
    """库里的数据区间（用于把 demo_as_of 夹到有效范围内）。"""
    p = Path(db_path)
    if not p.exists():
        raise MetricError("找不到数据库，请先运行 python -m app.data.seed")
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT MIN(dt), MAX(dt) FROM dim_date").fetchone()
    finally:
        con.close()
    if not row or not row[1]:
        raise MetricError("库里没有数据，请先运行 python -m app.data.seed")
    return date.fromisoformat(row[0]), date.fromisoformat(row[1])


def _last_date(db_path: str | Path) -> date:
    return _date_bounds(db_path)[1]


def resolve_as_of(db_path: str | Path, as_of: date | str | None = None, window_days: int = WINDOW_DAYS) -> date:
    """确定基准日：显式传入 > 配置里的 demo_as_of > 数据最新一天。

    夹到 [首日 + 2*window, 末日]，保证当前窗口与上期窗口都有数据。
    """
    first, last = _date_bounds(db_path)
    if as_of is None:
        raw = get_settings().demo_as_of.strip()
        try:
            as_of = date.fromisoformat(raw) if raw else last
        except ValueError:
            as_of = last
    elif isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    lo = first + timedelta(days=2 * window_days)
    return max(lo, min(as_of, last))


def _query(
    db_path: str | Path,
    metrics: list[str],
    start: date,
    end: date,
    dims: list[str] | None = None,
    limit: int = 5000,
) -> tuple[list[str], list[list[Any]]]:
    spec = QuerySpec(
        metrics=metrics,
        dimensions=dims or [],
        time={"start": start.isoformat(), "end": end.isoformat()},
        limit=limit,
    )
    res = run(spec, db_path)
    return res.columns, res.rows


def _delta(cur: float | int | None, prev: float | int | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return (float(cur) - float(prev)) / abs(float(prev))


def _pct(v: float | None) -> float | None:
    return None if v is None else round(v * 100, 1)


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _score_from_delta(delta: float | None, *, invert: bool = False) -> int:
    """变化率 → 0~100 分项分。持平 75 分，好转加分，恶化扣分。"""
    if delta is None:
        return 75
    d = -delta if invert else delta
    return int(round(_clamp(75 + d * 120)))


def _grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _kpis(db_path: str | Path, cur: tuple[date, date], prev: tuple[date, date]) -> list[dict[str, Any]]:
    cols_cur, rows_cur = _query(db_path, KPI_METRICS, *cur)
    cols_prev, rows_prev = _query(db_path, KPI_METRICS, *prev)
    c = dict(zip(cols_cur, rows_cur[0] if rows_cur else []))
    p = dict(zip(cols_prev, rows_prev[0] if rows_prev else []))

    spec = [
        ("gmv", "成交额", "currency", False),
        ("paid_orders", "支付订单", "count", False),
        ("aov", "客单价", "currency", False),
        ("cvr", "转化率", "ratio", False),
        ("refund_rate", "退款率", "ratio", True),
        ("roi", "投放 ROI", "ratio", False),
    ]
    out = []
    for name, label, unit, invert in spec:
        d = _delta(c.get(name), p.get(name))
        out.append(
            {
                "name": name,
                "label": label,
                "unit": unit,
                "value": c.get(name),
                "prev": p.get(name),
                "delta_pct": _pct(d),
                "good": None if d is None else (d < 0 if invert else d > 0),
            }
        )
    return out


def _health(kpis: list[dict[str, Any]]) -> dict[str, Any]:
    by = {k["name"]: k for k in kpis}
    traffic = _score_from_delta(_delta(by["gmv"]["value"], by["gmv"]["prev"]))
    conversion = _score_from_delta(by["cvr"]["delta_pct"] / 100 if by["cvr"]["delta_pct"] is not None else None)
    reputation = _score_from_delta(
        by["refund_rate"]["delta_pct"] / 100 if by["refund_rate"]["delta_pct"] is not None else None,
        invert=True,
    )
    total = int(round(traffic * 0.3 + conversion * 0.45 + reputation * 0.25))
    return {
        "score": total,
        "grade": _grade(total),
        "parts": {"traffic": traffic, "conversion": conversion, "reputation": reputation},
    }


def _scan_cards(db_path: str | Path, cur: tuple[date, date], prev: tuple[date, date]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for dim in ("style", "channel_id", "sku_id"):
        cols_c, rows_c = _query(db_path, SCAN_METRICS, *cur, [dim])
        cols_p, rows_p = _query(db_path, SCAN_METRICS, *prev, [dim])
        prev_map = {r[0]: dict(zip(cols_p, r)) for r in rows_p}
        for row in rows_c:
            rec = dict(zip(cols_c, row))
            key = rec[dim]
            before = prev_map.get(key)
            if not before or (rec.get("paid_orders") or 0) < VOLUME_FLOOR_ORDERS:
                continue
            for metric, code, title in RULES:
                d = _delta(rec.get(metric), before.get(metric))
                if d is None or d > -DELTA_TRIGGER:
                    continue
                impact = max(0.0, float(before.get("gmv") or 0) - float(rec.get("gmv") or 0))
                cards.append(
                    {
                        "code": code,
                        "title": title,
                        "dim": dim,
                        "dim_label": DIM_LABELS.get(dim, dim),
                        "dim_value": str(key),
                        "metric": metric,
                        "cur": rec.get(metric),
                        "prev": before.get(metric),
                        "delta_pct": _pct(d),
                        "impact_gmv": round(impact, 2),
                        "severity": "high" if d <= -0.35 else "mid",
                        "source": "rules-v0",
                    }
                )
                break
    cards.sort(key=lambda c: (c["severity"] != "high", -c["impact_gmv"]))
    return cards[:MAX_CARDS]


def _trend(db_path: str | Path, as_of: date) -> list[dict[str, Any]]:
    start = as_of - timedelta(days=TREND_DAYS - 1)
    cols, rows = _query(db_path, ["gmv", "paid_orders"], start, as_of, ["dt"], limit=TREND_DAYS)
    return [{"dt": r[0], "gmv": r[1], "paid_orders": r[2]} for r in rows]


def _by_style(db_path: str | Path, as_of: date) -> list[dict[str, Any]]:
    start = as_of - timedelta(days=TREND_DAYS - 1)
    cols, rows = _query(db_path, ["gmv", "paid_orders", "cvr"], start, as_of, ["style"], limit=50)
    out = [{"style": r[0], "gmv": r[1], "paid_orders": r[2], "cvr": r[3]} for r in rows]
    out.sort(key=lambda x: -(x["gmv"] or 0))
    return out


def build_overview(
    db_path: str | Path,
    window_days: int = WINDOW_DAYS,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    as_of_d = resolve_as_of(db_path, as_of, window_days)
    cur = (as_of_d - timedelta(days=window_days - 1), as_of_d)
    prev = (cur[0] - timedelta(days=window_days), cur[0] - timedelta(days=1))

    kpis = _kpis(db_path, cur, prev)
    return {
        "as_of": as_of_d.isoformat(),
        "window": {
            "days": window_days,
            "start": cur[0].isoformat(),
            "end": cur[1].isoformat(),
            "prev_start": prev[0].isoformat(),
            "prev_end": prev[1].isoformat(),
        },
        "kpis": kpis,
        "health": _health(kpis),
        "cards": _scan_cards(db_path, cur, prev),
        "trend": _trend(db_path, as_of_d),
        "by_style": _by_style(db_path, as_of_d),
        "meta": {
            "engine": "rules-v0",
            "note": "W1 规则版异常扫描，W2 由归因引擎替换；卡片根因码为疑似，仅用于界面联通。",
        },
    }
