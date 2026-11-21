"""T7 问数套件：20 条 QuerySpec 用例，用独立 SQL 做真值对账。

设计
----
- 每条用例是一份 ``QuerySpec``（交给语义层执行）+ 一条**独立对账 SQL**
  （只读 fact/dim 表，绝不引用语义层视图），再按列名逐值比对。
- 时间窗不写死：``end = SELECT MAX(dt) FROM dim_date``，``start = end - (N-1) 天``，
  因此真库（180 天）和测试小库（30 天）都能跑。
- 过滤值也不写死：用确定性 SQL（``ORDER BY COUNT(*) DESC, 值``）从库里取一个
  在当前时间窗内非空的样本（style / channel / price_band / category_l1 / sku），
  保证小库上同样有数据。
- ``compare_case(columns, rows, payload)`` 是纯函数：期望值由调用方放在
  ``payload["expected"] = {"columns": [...], "rows": [[...]]}``。``run()`` 执行
  独立 SQL 后把结果注入 ``payload`` 的副本再调用它；``Case.payload`` 本身仍只
  有 ``label / spec / sql / by_dim`` 四个键。

覆盖（14 条无维度 + 6 条带维度，目录内 24 个指标全覆盖）
-------------------------------------------------
======  ======  ======================  =========================  ======
case    窗口    指标                     维度 / 过滤                说明
======  ======  ======================  =========================  ======
Q01     7d      gmv/paid_orders/aov      -                          整体成交
Q02     7d      uv/pv/ctr/cvr            -                          整体流量
Q03     14d     impressions/clicks/      -                          曝光点击
                add_cart_rate
Q04     14d     detail_uv/add_cart_uv/   -                          详情页与加购
                pay_buyers
Q05     30d     gmv/paid_orders/aov/     filter style=              单风格成交
                gross_margin
Q06     30d     ad_spend/roi/cpc         filter channel_id=         单渠道投放
Q07     90d     neg_review_rate/dsr/    filter price_band=         单价格带口碑
                review_cnt
Q08     7d      uv/pv/ctr                filter category_l1=        单品类流量
Q09     90d     unit_price/refund_rate/  -                          退款/毛利
                return_rate/gross_margin
Q10     30d     stockout_rate/gmv        -                          库存与成交
Q11     14d     gmv/uv                   -                          跨源合并
Q12     90d     aov/dsr                  -                          跨源合并
Q13     7d      roi/stockout_rate        -                          跨源合并
Q14     30d     cvr/refund_rate          -                          跨源合并
Q15     7d      gmv/paid_orders/aov      dim style                  按风格
Q16     14d     uv/pv/ctr                dim channel_id             按渠道
Q17     30d     gmv/refund_rate          dim price_band             按价格带
Q18     7d      gmv/paid_orders/aov/     dim sku_id + filter        单 SKU 明细
                gross_margin             sku_id=
Q19     90d     ad_spend/roi/cpc         dim month                  按月投放
Q20     30d     neg_review_rate/dsr      dim style                  按风格口碑
======  ======  ======================  =========================  ======
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.eval.metrics import numeric_close
from app.eval.types import Case, CaseResult, SuiteResult
from app.metrics import run as run_query
from app.metrics.spec import QuerySpec

SUITE = "query"
CASE_COUNT = 20
REL_TOL = 1e-5
ABS_TOL = 1e-4

# 独立对账 SQL 只允许出现这些物理表（含子查询里的 fact_refunds / fact_traffic_daily）。
ALLOWED_TABLES = frozenset(
    {
        "fact_orders",
        "fact_traffic_daily",
        "fact_refunds",
        "fact_ads",
        "fact_reviews",
        "inventory_snapshot",
        "dim_sku",
        "dim_date",
        "dim_channel",
    }
)

_METRIC_COLUMNS = frozenset(
    {
        "gmv", "paid_orders", "pay_buyers", "aov", "unit_price", "gross_margin",
        "uv", "pv", "impressions", "clicks", "ctr", "cvr", "detail_uv",
        "add_cart_uv", "add_cart_rate", "refund_rate", "return_rate",
        "neg_review_rate", "review_cnt", "ad_spend", "roi", "cpc",
        "stockout_rate", "dsr",
    }
)


# ---------------------------------------------------------------------------
# 只读连接与确定性取数
# ---------------------------------------------------------------------------


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA query_only = ON")
    return con


def _scalar(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = con.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _window(end: str, days: int) -> tuple[str, str]:
    last = date.fromisoformat(end)
    first = last - timedelta(days=days - 1)
    return first.isoformat(), last.isoformat()


def _pick(con: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> str:
    value = _scalar(con, sql, params)
    if value is None:
        raise RuntimeError(f"样本取值失败（时间窗内无数据）：{sql}")
    return str(value)


def _sql_str(value: str) -> str:
    """把库里的维度值安全地嵌进独立 SQL（单引号翻倍）。"""
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# 用例定义
# ---------------------------------------------------------------------------


def _spec(
    metrics: list[str],
    start: str,
    end: str,
    *,
    dimensions: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    order_by: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "metrics": list(metrics),
        "dimensions": list(dimensions or []),
        "filters": list(filters or []),
        "time": {"start": start, "end": end},
        "limit": 5000,
    }
    if order_by:
        spec["order_by"] = list(order_by)
    return spec


def _case(case_id: str, label: str, spec: dict[str, Any], sql: str, by_dim: bool) -> Case:
    return Case(
        case_id=case_id,
        suite=SUITE,
        payload={"label": label, "spec": spec, "sql": sql, "by_dim": by_dim},
        note=label,
    )


def _cases(db_path: str | Path) -> list[Case]:
    """生成 20 条确定性用例；只读库取 end 日期与过滤样本，不随机。"""
    with closing(_connect(db_path)) as con:
        end = _scalar(con, "SELECT MAX(dt) FROM dim_date")
        if not end:
            raise RuntimeError(f"dim_date 为空，无法确定时间窗: {db_path}")
        end = str(end)
        w7 = _window(end, 7)
        w14 = _window(end, 14)
        w30 = _window(end, 30)
        w90 = _window(end, 90)

        # 过滤样本：取窗口内出现次数最多的值（并列时按值稳定排序），保证非空。
        style = _pick(
            con,
            "SELECT s.style FROM fact_orders o JOIN dim_sku s ON s.sku_id = o.sku_id "
            "WHERE o.dt BETWEEN ? AND ? GROUP BY s.style "
            "ORDER BY COUNT(*) DESC, s.style LIMIT 1",
            w30,
        )
        channel = _pick(
            con,
            "SELECT channel_id FROM fact_ads WHERE dt BETWEEN ? AND ? GROUP BY channel_id "
            "ORDER BY COUNT(*) DESC, channel_id LIMIT 1",
            w30,
        )
        price_band = _pick(
            con,
            "SELECT s.price_band FROM fact_reviews r JOIN dim_sku s ON s.sku_id = r.sku_id "
            "WHERE r.dt BETWEEN ? AND ? GROUP BY s.price_band "
            "ORDER BY COUNT(*) DESC, s.price_band LIMIT 1",
            w90,
        )
        category_l1 = _pick(
            con,
            "SELECT s.category_l1 FROM fact_traffic_daily t JOIN dim_sku s ON s.sku_id = t.sku_id "
            "WHERE t.dt BETWEEN ? AND ? GROUP BY s.category_l1 "
            "ORDER BY COUNT(*) DESC, s.category_l1 LIMIT 1",
            w7,
        )
        sku_id = _pick(
            con,
            "SELECT sku_id FROM fact_orders WHERE dt BETWEEN ? AND ? GROUP BY sku_id "
            "ORDER BY COUNT(*) DESC, sku_id LIMIT 1",
            w7,
        )

    s7, e7 = w7
    s14, e14 = w14
    s30, e30 = w30
    s90, e90 = w90
    style_lit = _sql_str(style)
    channel_lit = _sql_str(channel)
    price_band_lit = _sql_str(price_band)
    category_lit = _sql_str(category_l1)
    sku_lit = _sql_str(sku_id)
    d7 = f"o.dt BETWEEN '{s7}' AND '{e7}'"
    t7 = f"t.dt BETWEEN '{s7}' AND '{e7}'"
    t14 = f"t.dt BETWEEN '{s14}' AND '{e14}'"
    d30 = f"o.dt BETWEEN '{s30}' AND '{e30}'"
    a30 = f"a.dt BETWEEN '{s30}' AND '{e30}'"
    d90 = f"o.dt BETWEEN '{s90}' AND '{e90}'"
    a90 = f"a.dt BETWEEN '{s90}' AND '{e90}'"
    r90 = f"r.dt BETWEEN '{s90}' AND '{e90}'"
    d_order90 = f"dt BETWEEN '{s90}' AND '{e90}'"

    cases: list[Case] = [
        _case(
            "Q01",
            "7天整体成交",
            _spec(["gmv", "paid_orders", "aov"], *w7),
            f"""
            SELECT SUM(pay_amount) AS gmv,
                   COUNT(*) AS paid_orders,
                   SUM(pay_amount) * 1.0 / NULLIF(COUNT(*), 0) AS aov
              FROM fact_orders
             WHERE dt BETWEEN '{s7}' AND '{e7}'
            """,
            False,
        ),
        _case(
            "Q02",
            "7天整体流量",
            _spec(["uv", "pv", "ctr", "cvr"], *w7),
            f"""
            SELECT SUM(detail_uv) AS uv,
                   SUM(clicks) AS pv,
                   SUM(clicks) * 1.0 / NULLIF(SUM(impressions), 0) AS ctr,
                   SUM(paid_orders) * 1.0 / NULLIF(SUM(detail_uv), 0) AS cvr
              FROM fact_traffic_daily
             WHERE dt BETWEEN '{s7}' AND '{e7}'
            """,
            False,
        ),
        _case(
            "Q03",
            "14天曝光点击",
            _spec(["impressions", "clicks", "add_cart_rate"], *w14),
            f"""
            SELECT SUM(impressions) AS impressions,
                   SUM(clicks) AS clicks,
                   SUM(add_cart_uv) * 1.0 / NULLIF(SUM(detail_uv), 0) AS add_cart_rate
              FROM fact_traffic_daily
             WHERE dt BETWEEN '{s14}' AND '{e14}'
            """,
            False,
        ),
        _case(
            "Q04",
            "14天详情页与加购",
            _spec(["detail_uv", "add_cart_uv", "pay_buyers"], *w14),
            f"""
            SELECT SUM(detail_uv) AS detail_uv,
                   SUM(add_cart_uv) AS add_cart_uv,
                   SUM(pay_uv) AS pay_buyers
              FROM fact_traffic_daily
             WHERE dt BETWEEN '{s14}' AND '{e14}'
            """,
            False,
        ),
        _case(
            "Q05",
            "30天单风格成交",
            _spec(
                ["gmv", "paid_orders", "aov", "gross_margin"],
                *w30,
                filters=[{"field": "style", "op": "=", "value": style}],
            ),
            f"""
            SELECT SUM(o.pay_amount) AS gmv,
                   COUNT(*) AS paid_orders,
                   SUM(o.pay_amount) * 1.0 / NULLIF(COUNT(*), 0) AS aov,
                   (SUM(o.pay_amount) - SUM(s.cost * o.qty)) * 1.0
                       / NULLIF(SUM(o.pay_amount), 0) AS gross_margin
              FROM fact_orders o
              JOIN dim_sku s ON s.sku_id = o.sku_id
             WHERE {d30} AND s.style = {style_lit}
            """,
            False,
        ),
        _case(
            "Q06",
            "30天单渠道投放",
            _spec(
                ["ad_spend", "roi", "cpc"],
                *w30,
                filters=[{"field": "channel_id", "op": "=", "value": channel}],
            ),
            f"""
            SELECT SUM(a.spend) AS ad_spend,
                   SUM(COALESCE(t.gmv, 0)) * 1.0 / NULLIF(SUM(a.spend), 0) AS roi,
                   SUM(a.spend) * 1.0 / NULLIF(SUM(a.clicks), 0) AS cpc
              FROM fact_ads a
              LEFT JOIN fact_traffic_daily t
                     ON t.dt = a.dt AND t.sku_id = a.sku_id AND t.channel_id = a.channel_id
             WHERE {a30} AND a.channel_id = {channel_lit}
            """,
            False,
        ),
        _case(
            "Q07",
            "90天单价格带口碑",
            _spec(
                ["neg_review_rate", "dsr", "review_cnt"],
                *w90,
                filters=[{"field": "price_band", "op": "=", "value": price_band}],
            ),
            f"""
            SELECT SUM(CASE WHEN r.rating <= 2 THEN 1 ELSE 0 END) * 1.0
                       / NULLIF(COUNT(*), 0) AS neg_review_rate,
                   AVG(r.rating) AS dsr,
                   COUNT(*) AS review_cnt
              FROM fact_reviews r
              JOIN dim_sku s ON s.sku_id = r.sku_id
             WHERE {r90} AND s.price_band = {price_band_lit}
            """,
            False,
        ),
        _case(
            "Q08",
            "7天单品类流量",
            _spec(
                ["uv", "pv", "ctr"],
                *w7,
                filters=[{"field": "category_l1", "op": "=", "value": category_l1}],
            ),
            f"""
            SELECT SUM(t.detail_uv) AS uv,
                   SUM(t.clicks) AS pv,
                   SUM(t.clicks) * 1.0 / NULLIF(SUM(t.impressions), 0) AS ctr
              FROM fact_traffic_daily t
              JOIN dim_sku s ON s.sku_id = t.sku_id
             WHERE {t7} AND s.category_l1 = {category_lit}
            """,
            False,
        ),
        _case(
            "Q09",
            "90天退款与毛利",
            _spec(["unit_price", "refund_rate", "return_rate", "gross_margin"], *w90),
            f"""
            SELECT AVG(o.unit_price) AS unit_price,
                   SUM(CASE WHEN r.order_id IS NOT NULL THEN 1 ELSE 0 END) * 1.0
                       / NULLIF(COUNT(*), 0) AS refund_rate,
                   SUM(CASE WHEN r.reason_code = 'SIZE_SMALL' THEN 1 ELSE 0 END) * 1.0
                       / NULLIF(COUNT(*), 0) AS return_rate,
                   (SUM(o.pay_amount) - SUM(s.cost * o.qty)) * 1.0
                       / NULLIF(SUM(o.pay_amount), 0) AS gross_margin
              FROM fact_orders o
              LEFT JOIN (SELECT DISTINCT order_id, reason_code FROM fact_refunds) r
                     ON r.order_id = o.order_id
              JOIN dim_sku s ON s.sku_id = o.sku_id
             WHERE {d90}
            """,
            False,
        ),
        _case(
            "Q10",
            "30天库存与成交",
            _spec(["stockout_rate", "gmv"], *w30),
            f"""
            SELECT (SELECT AVG(stockout_flag) FROM inventory_snapshot
                     WHERE dt BETWEEN '{s30}' AND '{e30}') AS stockout_rate,
                   (SELECT SUM(pay_amount) FROM fact_orders
                     WHERE dt BETWEEN '{s30}' AND '{e30}') AS gmv
            """,
            False,
        ),
        _case(
            "Q11",
            "14天成交与访客（跨源）",
            _spec(["gmv", "uv"], *w14),
            f"""
            SELECT (SELECT SUM(pay_amount) FROM fact_orders
                     WHERE dt BETWEEN '{s14}' AND '{e14}') AS gmv,
                   (SELECT SUM(detail_uv) FROM fact_traffic_daily
                     WHERE dt BETWEEN '{s14}' AND '{e14}') AS uv
            """,
            False,
        ),
        _case(
            "Q12",
            "90天客单价与评分（跨源）",
            _spec(["aov", "dsr"], *w90),
            f"""
            SELECT (SELECT SUM(pay_amount) * 1.0 / NULLIF(COUNT(*), 0) FROM fact_orders
                     WHERE {d_order90}) AS aov,
                   (SELECT AVG(rating) FROM fact_reviews
                     WHERE {d_order90}) AS dsr
            """,
            False,
        ),
        _case(
            "Q13",
            "7天投放与断货（跨源）",
            _spec(["roi", "stockout_rate"], *w7),
            f"""
            SELECT (SELECT SUM(COALESCE(t.gmv, 0)) * 1.0 / NULLIF(SUM(a.spend), 0)
                      FROM fact_ads a
                      LEFT JOIN fact_traffic_daily t
                             ON t.dt = a.dt AND t.sku_id = a.sku_id
                            AND t.channel_id = a.channel_id
                     WHERE a.dt BETWEEN '{s7}' AND '{e7}') AS roi,
                   (SELECT AVG(stockout_flag) FROM inventory_snapshot
                     WHERE dt BETWEEN '{s7}' AND '{e7}') AS stockout_rate
            """,
            False,
        ),
        _case(
            "Q14",
            "30天转化与退款（跨源）",
            _spec(["cvr", "refund_rate"], *w30),
            f"""
            SELECT (SELECT SUM(paid_orders) * 1.0 / NULLIF(SUM(detail_uv), 0)
                      FROM fact_traffic_daily
                     WHERE dt BETWEEN '{s30}' AND '{e30}') AS cvr,
                   (SELECT SUM(CASE WHEN r.order_id IS NOT NULL THEN 1 ELSE 0 END) * 1.0
                               / NULLIF(COUNT(*), 0)
                      FROM fact_orders o
                      LEFT JOIN (SELECT DISTINCT order_id FROM fact_refunds) r
                             ON r.order_id = o.order_id
                     WHERE {d30}) AS refund_rate
            """,
            False,
        ),
        _case(
            "Q15",
            "7天按风格成交",
            _spec(
                ["gmv", "paid_orders", "aov"],
                *w7,
                dimensions=["style"],
                order_by=[{"field": "gmv", "dir": "desc"}],
            ),
            f"""
            SELECT s.style AS style,
                   SUM(o.pay_amount) AS gmv,
                   COUNT(*) AS paid_orders,
                   SUM(o.pay_amount) * 1.0 / NULLIF(COUNT(*), 0) AS aov
              FROM fact_orders o
              JOIN dim_sku s ON s.sku_id = o.sku_id
             WHERE {d7}
             GROUP BY s.style
            """,
            True,
        ),
        _case(
            "Q16",
            "14天按渠道流量",
            _spec(["uv", "pv", "ctr"], *w14, dimensions=["channel_id"]),
            f"""
            SELECT t.channel_id AS channel_id,
                   SUM(t.detail_uv) AS uv,
                   SUM(t.clicks) AS pv,
                   SUM(t.clicks) * 1.0 / NULLIF(SUM(t.impressions), 0) AS ctr
              FROM fact_traffic_daily t
             WHERE {t14}
             GROUP BY t.channel_id
            """,
            True,
        ),
        _case(
            "Q17",
            "30天按价格带成交",
            _spec(["gmv", "refund_rate"], *w30, dimensions=["price_band"]),
            f"""
            SELECT s.price_band AS price_band,
                   SUM(o.pay_amount) AS gmv,
                   SUM(CASE WHEN r.order_id IS NOT NULL THEN 1 ELSE 0 END) * 1.0
                       / NULLIF(COUNT(*), 0) AS refund_rate
              FROM fact_orders o
              JOIN dim_sku s ON s.sku_id = o.sku_id
              LEFT JOIN (SELECT DISTINCT order_id FROM fact_refunds) r
                     ON r.order_id = o.order_id
             WHERE {d30}
             GROUP BY s.price_band
            """,
            True,
        ),
        _case(
            "Q18",
            "7天单SKU明细",
            _spec(
                ["gmv", "paid_orders", "aov", "gross_margin"],
                *w7,
                dimensions=["sku_id"],
                filters=[{"field": "sku_id", "op": "=", "value": sku_id}],
            ),
            f"""
            SELECT o.sku_id AS sku_id,
                   SUM(o.pay_amount) AS gmv,
                   COUNT(*) AS paid_orders,
                   SUM(o.pay_amount) * 1.0 / NULLIF(COUNT(*), 0) AS aov,
                   (SUM(o.pay_amount) - SUM(s.cost * o.qty)) * 1.0
                       / NULLIF(SUM(o.pay_amount), 0) AS gross_margin
              FROM fact_orders o
              JOIN dim_sku s ON s.sku_id = o.sku_id
             WHERE {d7} AND o.sku_id = {sku_lit}
             GROUP BY o.sku_id
            """,
            True,
        ),
        _case(
            "Q19",
            "90天按月投放",
            _spec(
                ["ad_spend", "roi", "cpc"],
                *w90,
                dimensions=["month"],
                order_by=[{"field": "ad_spend", "dir": "desc"}],
            ),
            f"""
            SELECT d.month AS month,
                   SUM(a.spend) AS ad_spend,
                   SUM(COALESCE(t.gmv, 0)) * 1.0 / NULLIF(SUM(a.spend), 0) AS roi,
                   SUM(a.spend) * 1.0 / NULLIF(SUM(a.clicks), 0) AS cpc
              FROM fact_ads a
              LEFT JOIN fact_traffic_daily t
                     ON t.dt = a.dt AND t.sku_id = a.sku_id AND t.channel_id = a.channel_id
              JOIN dim_date d ON d.dt = a.dt
             WHERE {a90}
             GROUP BY d.month
            """,
            True,
        ),
        _case(
            "Q20",
            "30天按风格口碑",
            _spec(["neg_review_rate", "dsr"], *w30, dimensions=["style"]),
            f"""
            SELECT s.style AS style,
                   SUM(CASE WHEN r.rating <= 2 THEN 1 ELSE 0 END) * 1.0
                       / NULLIF(COUNT(*), 0) AS neg_review_rate,
                   AVG(r.rating) AS dsr
              FROM fact_reviews r
              JOIN dim_sku s ON s.sku_id = r.sku_id
             WHERE r.dt BETWEEN '{s30}' AND '{e30}'
             GROUP BY s.style
            """,
            True,
        ),
    ]
    assert len(cases) == CASE_COUNT
    return cases


def cases(db_path: str | Path) -> list[Case]:
    """对外入口：20 条确定性用例。"""
    return _cases(db_path)


# ---------------------------------------------------------------------------
# 比对（纯函数）
# ---------------------------------------------------------------------------


def _expected_of(payload: dict[str, Any]) -> tuple[list[str], list[list[Any]]] | None:
    expected = payload.get("expected")
    if not isinstance(expected, dict):
        return None
    columns = expected.get("columns")
    rows = expected.get("rows")
    if not isinstance(columns, (list, tuple)) or not columns or rows is None:
        return None
    return [str(c) for c in columns], [list(r) for r in rows]


def _close(actual: Any, expected: Any) -> bool:
    """数值走容差；NULL 只与 NULL 相等；其余按字面值比较。"""
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual == expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return numeric_close(actual, expected, rel=REL_TOL, abs_=ABS_TOL)
    return actual == expected


def _rows_for_detail(rows: list[list[Any]], cap: int = 20) -> list[list[Any]]:
    return rows[:cap]


def compare_case(columns: list[str], rows: list[list[Any]], payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """把语义层结果与 ``payload["expected"]`` 对账，返回 (ok, detail)。

    ``columns`` / ``rows`` 是语义层（engine）的结果；期望值放在
    ``payload["expected"] = {"columns": [...], "rows": [[...], ...]}``。
    无维度时要求两边各恰好 1 行；有维度时先按维度值建 dict，再逐指标比数值。
    """
    label = payload.get("label", "")
    sql = payload.get("sql", "")
    actual_columns = [str(c) for c in columns]
    actual_rows = [list(r) for r in rows]
    detail: dict[str, Any] = {
        "label": label,
        "sql": sql,
        "expected": None,
        "actual": {"columns": actual_columns, "rows": _rows_for_detail(actual_rows)},
        "diffs": [],
    }
    exp = _expected_of(payload)
    if exp is None:
        detail["diffs"].append("payload 缺少 expected：应为 {'columns': [...], 'rows': [[...]]}")
        return False, detail

    exp_columns, exp_rows = exp
    detail["expected"] = {"columns": exp_columns, "rows": _rows_for_detail(exp_rows)}
    diffs: list[str] = detail["diffs"]

    missing = [c for c in exp_columns if c not in actual_columns]
    extra = [c for c in actual_columns if c not in exp_columns]
    if missing:
        diffs.append(f"缺列: {missing}")
    if extra:
        diffs.append(f"多列: {extra}")
    if diffs:
        return False, detail

    spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
    dims = [d for d in (spec.get("dimensions") or []) if d in exp_columns]
    metrics = [c for c in exp_columns if c not in dims]
    by_dim = bool(payload.get("by_dim"))

    if not by_dim:
        if len(exp_rows) != 1:
            diffs.append(f"期望结果应为 1 行，实际 {len(exp_rows)} 行")
        if len(actual_rows) != 1:
            diffs.append(f"语义层应返回 1 行，实际 {len(actual_rows)} 行")
        if diffs:
            return False, detail
        for name in metrics:
            got = actual_rows[0][actual_columns.index(name)]
            want = exp_rows[0][exp_columns.index(name)]
            if not _close(got, want):
                diffs.append(f"{name}: 实际 {got!r} ≠ 期望 {want!r}")
        return (not diffs), detail

    if not dims:
        diffs.append("by_dim=True 但 spec.dimensions 为空")
        return False, detail

    def _table(rows_: list[list[Any]], columns_: list[str]) -> dict[tuple[Any, ...], list[Any]]:
        table: dict[tuple[Any, ...], list[Any]] = {}
        for row_ in rows_:
            key = tuple(row_[columns_.index(d)] for d in dims)
            table[key] = row_
        return table

    exp_table = _table(exp_rows, exp_columns)
    act_table = _table(actual_rows, actual_columns)
    if len(exp_rows) != len(actual_rows):
        diffs.append(f"行数不一致: 语义层 {len(actual_rows)} 行，期望 {len(exp_rows)} 行")
    missing_keys = [k for k in exp_table if k not in act_table]
    extra_keys = [k for k in act_table if k not in exp_table]
    if missing_keys:
        diffs.append(f"缺维度值: {missing_keys[:10]}")
    if extra_keys:
        diffs.append(f"多维度值: {extra_keys[:10]}")
    for key in exp_table:
        if key not in act_table:
            continue
        for name in metrics:
            got = act_table[key][actual_columns.index(name)]
            want = exp_table[key][exp_columns.index(name)]
            if not _close(got, want):
                diffs.append(f"{key} {name}: 实际 {got!r} ≠ 期望 {want!r}")
    return (not diffs), detail


# ---------------------------------------------------------------------------
# 套件执行
# ---------------------------------------------------------------------------


def _run_one(case: Case, db_path: Path) -> CaseResult:
    payload = case.payload
    try:
        spec = QuerySpec(**payload["spec"])
        got = run_query(spec, db_path)
        with closing(_connect(db_path)) as con:
            cursor = con.execute(payload["sql"])
            exp_columns = [str(d[0]) for d in cursor.description or []]
            exp_rows = [list(r) for r in cursor.fetchall()]
        enriched = dict(payload)
        enriched["expected"] = {"columns": exp_columns, "rows": exp_rows}
        ok, detail = compare_case(got.columns, got.rows, enriched)
    except Exception as exc:  # noqa: BLE001 - 单条用例失败要落成 CaseResult，不能中断套件
        ok = False
        detail = {
            "label": payload.get("label", ""),
            "sql": payload.get("sql", ""),
            "expected": None,
            "actual": None,
            "diffs": [f"{type(exc).__name__}: {exc}"],
        }
    return CaseResult(case_id=case.case_id, ok=ok, score=1.0 if ok else 0.0, detail=detail)


def run(db_path: str | Path, *, limit: int | None = None) -> SuiteResult:
    """跑套件；``limit`` 只取前 N 条（其余行为不变）。"""
    started = time.perf_counter()
    db_path = Path(db_path)
    todo = cases(db_path)
    if limit is not None:
        todo = todo[: max(0, int(limit))]
    results = [_run_one(case, db_path) for case in todo]
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return SuiteResult(
        suite=SUITE,
        cases=results,
        elapsed_ms=elapsed_ms,
        meta={"engine": "sql-truth-v1", "db": str(db_path)},
    )
