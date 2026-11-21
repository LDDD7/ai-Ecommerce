"""语义层与编译器的测试：目录、白名单、参数绑定、只读。"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from pydantic import ValidationError

from app.metrics import QuerySpec, build, load_catalog, run
from app.metrics.spec import MAX_DAYS, MetricError


def test_catalog_has_all_metrics():
    cat = load_catalog()
    assert cat.version >= 1
    # 23 个业务指标 + review_cnt（差评率的分母，只当样本量用）
    assert len(cat.metrics) == 24
    for must in ("gmv", "cvr", "ctr", "refund_rate", "roi", "stockout_rate", "neg_review_rate", "review_cnt"):
        assert must in cat.metrics


def test_catalog_dims_are_view_columns():
    cat = load_catalog()
    for m in cat.metrics.values():
        assert "dt" in m.allowed_dims
        assert m.allowed_dims <= set(m.allowed_dims)


def test_build_binds_values_not_interpolates():
    spec = QuerySpec(
        metrics=["gmv"],
        dimensions=["style"],
        time={"start": "2026-11-01", "end": "2026-11-10"},
        filters=[{"field": "style", "op": "=", "value": "通勤"}],
    )
    qs = build(spec)
    assert len(qs) == 1
    sql = qs[0].sql
    assert "?" in sql
    assert "通勤" not in sql
    assert qs[0].params == ["2026-11-01", "2026-11-10", "通勤"]
    assert "SELECT" in sql.upper() and "DROP" not in sql.upper()


def test_reject_cross_metric_dimension():
    spec = QuerySpec(metrics=["gmv", "dsr"], dimensions=["channel_id"])
    with pytest.raises(MetricError):
        build(spec)


def test_reject_measure_as_dimension():
    spec = QuerySpec(metrics=["gmv"], dimensions=["rating"])
    with pytest.raises(MetricError):
        build(spec)


def test_reject_unknown_metric():
    spec = QuerySpec(metrics=["not_a_metric"])
    with pytest.raises(MetricError):
        build(spec)


def test_limit_capped_by_contract():
    with pytest.raises(ValidationError):
        QuerySpec(metrics=["gmv"], limit=999999)


def test_time_span_capped():
    assert (date(2026, 11, 1) - date(2025, 1, 1)).days > MAX_DAYS
    with pytest.raises(ValidationError):
        QuerySpec(
            metrics=["gmv"],
            time={"start": "2025-01-01", "end": "2026-11-01"},
        )


def test_injection_value_is_parameter(db_path):
    spec = QuerySpec(
        metrics=["gmv"],
        dimensions=["style"],
        time={"start": "2026-11-01", "end": "2026-11-10"},
        filters=[{"field": "style", "op": "=", "value": "x'; DROP TABLE fact_orders; --"}],
    )
    res = run(spec, db_path)
    assert res.rows == []
    # 表还在
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0] > 0
    con.close()


def test_run_returns_metrics_in_order(db_path):
    spec = QuerySpec(
        metrics=["gmv", "paid_orders", "aov"],
        time={"start": "2026-11-01", "end": "2026-11-18"},
    )
    res = run(spec, db_path)
    assert res.columns == ["gmv", "paid_orders", "aov"]
    gmv, orders, aov = res.rows[0]
    assert gmv > 0 and orders > 0
    assert abs(aov - gmv / orders) < 1e-6
    assert res.meta["sql_statements"] == 1


def test_run_orders_and_limit(db_path):
    spec = QuerySpec(
        metrics=["gmv"],
        dimensions=["sku_id"],
        time={"start": "2026-11-01", "end": "2026-11-18"},
        order_by=[{"field": "gmv", "dir": "desc"}],
        limit=3,
    )
    res = run(spec, db_path)
    assert len(res.rows) == 3
    values = [r[1] for r in res.rows]
    assert values == sorted(values, reverse=True)
    assert res.meta["truncated"] is True


def test_readonly_connection_rejects_writes(db_path):
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM fact_orders")
    con.close()
