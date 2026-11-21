"""T7 问数套件（``app/eval/suites/query_suite.py``）的离线契约测试。

只使用 ``tests/conftest.py`` 生成的测试小库（20 SKU / 30 天），不访问网络；
``run(db_path)`` 的期望值来自套件自带的独立对账 SQL，因此这里只断言"全对"。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from app.eval.suites.query_suite import (
    ALLOWED_TABLES,
    CASE_COUNT,
    SUITE,
    cases,
    compare_case,
    run,
)
from app.eval.types import Case, SuiteResult
from app.metrics.spec import QuerySpec, load_catalog

TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)", re.IGNORECASE)
SEMANTIC_VIEW = re.compile(r"\b\w+_v\b", re.IGNORECASE)
REQUIRED_WINDOWS = {7, 14, 30, 90}
MIN_METRICS = 2
MAX_METRICS = 4

# 任务书点名的 23 个指标（目录后续可能继续扩充，例如 review_cnt）。
REQUIRED_METRICS = frozenset(
    {
        "gmv", "paid_orders", "pay_buyers", "aov", "unit_price", "gross_margin",
        "uv", "pv", "impressions", "clicks", "ctr", "cvr", "detail_uv",
        "add_cart_uv", "add_cart_rate", "refund_rate", "return_rate",
        "neg_review_rate", "ad_spend", "roi", "cpc", "stockout_rate", "dsr",
    }
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _tables(sql: str) -> list[str]:
    return [m.group(1).lower() for m in TABLE_REF.finditer(sql)]


def _window_days(spec: dict) -> int:
    start = date.fromisoformat(spec["time"]["start"])
    end = date.fromisoformat(spec["time"]["end"])
    return (end - start).days + 1


def _max_dt(db_path: Path) -> str:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        row = con.execute("SELECT MAX(dt) FROM dim_date").fetchone()
    assert row and row[0], "dim_date 为空"
    return str(row[0])


@pytest.fixture(scope="module")
def all_cases(db_path) -> list[Case]:
    return cases(db_path)


# ---------------------------------------------------------------------------
# 用例结构
# ---------------------------------------------------------------------------


def test_suite_and_case_shape(all_cases):
    assert SUITE == "query"
    assert CASE_COUNT == 20
    assert len(all_cases) == 20

    expected_ids = [f"Q{i:02d}" for i in range(1, 21)]
    assert [c.case_id for c in all_cases] == expected_ids
    for case in all_cases:
        assert isinstance(case, Case)
        assert case.suite == SUITE
        assert set(case.payload) == {"label", "spec", "sql", "by_dim"}
        assert isinstance(case.payload["label"], str) and case.payload["label"]
        assert isinstance(case.payload["sql"], str) and case.payload["sql"].strip()
        assert isinstance(case.payload["by_dim"], bool)
        assert case.note


def test_specs_pass_query_spec_validation(all_cases):
    for case in all_cases:
        spec_dict = case.payload["spec"]
        spec = QuerySpec(**spec_dict)
        assert spec.metrics == spec_dict["metrics"]
        assert spec.dimensions == spec_dict["dimensions"]
        assert MIN_METRICS <= len(spec.metrics) <= MAX_METRICS
        assert len(spec.dimensions) <= 3
        # by_dim 必须和 dimensions 是否为空严格对应。
        assert case.payload["by_dim"] is bool(spec.dimensions)
        assert spec.time.start.isoformat() == spec_dict["time"]["start"]
        assert spec.time.end.isoformat() == spec_dict["time"]["end"]


def test_integrity_sql_only_touches_physical_tables(all_cases):
    for case in all_cases:
        sql = case.payload["sql"]
        assert sql.lstrip().upper().startswith("SELECT"), case.case_id
        assert not SEMANTIC_VIEW.search(sql), f"{case.case_id} 引用了语义层视图: {sql}"
        refs = _tables(sql)
        assert refs, f"{case.case_id} 的对账 SQL 没有 FROM"
        unknown = sorted(set(refs) - ALLOWED_TABLES)
        assert not unknown, f"{case.case_id} 引用了非物理表: {unknown}"


def test_metrics_cover_whole_catalog(all_cases):
    catalog_metrics = set(load_catalog().metrics)
    assert len(catalog_metrics) >= 23
    covered = {m for case in all_cases for m in case.payload["spec"]["metrics"]}
    assert REQUIRED_METRICS <= covered
    # 新目录里的指标也要覆盖（当前含 review_cnt），防止语义层加了指标而套件漏测。
    assert catalog_metrics <= covered, f"未覆盖目录指标: {sorted(catalog_metrics - covered)}"
    assert not (covered - catalog_metrics), f"出现目录外指标: {sorted(covered - catalog_metrics)}"


def test_time_windows_cover_required_spans(db_path, all_cases):
    spans = {_window_days(case.payload["spec"]) for case in all_cases}
    assert REQUIRED_WINDOWS <= spans, f"缺少时间窗: {REQUIRED_WINDOWS - spans}"

    # end 必须取自 dim_date 的最大日期，不能写死。
    max_dt = _max_dt(db_path)
    for case in all_cases:
        end = case.payload["spec"]["time"]["end"]
        assert end == max_dt, f"{case.case_id} 的 end={end} 不等于 MAX(dt)={max_dt}"


def test_dimension_and_filter_coverage(all_cases):
    by_dim = [c for c in all_cases if c.payload["by_dim"]]
    assert len(by_dim) == 6
    dim_names = {d for c in by_dim for d in c.payload["spec"]["dimensions"]}
    assert {"style", "channel_id", "price_band", "sku_id"} <= dim_names

    filtered = [c for c in all_cases if c.payload["spec"]["filters"]]
    assert len(filtered) >= 4
    filter_fields = {f["field"] for c in filtered for f in c.payload["spec"]["filters"]}
    assert {"style", "channel_id", "price_band", "category_l1"} <= filter_fields

    for case in filtered:
        for flt in case.payload["spec"]["filters"]:
            assert flt["op"] == "=", case.case_id
            value = flt["value"]
            assert isinstance(value, str) and value
            # 过滤值必须真的落进对账 SQL（单引号按 SQL 规则翻倍）。
            assert value.replace("'", "''") in case.payload["sql"]

    sku_dim_cases = [c for c in by_dim if "sku_id" in c.payload["spec"]["dimensions"]]
    assert sku_dim_cases, "缺少 sku_id 维度的用例"
    for case in sku_dim_cases:
        sku_filters = [f for f in case.payload["spec"]["filters"] if f["field"] == "sku_id"]
        assert len(sku_filters) == 1, f"{case.case_id} 应按单个 sku_id 过滤"
        assert sku_filters[0]["op"] == "="


def test_cases_are_deterministic(db_path, all_cases):
    again = cases(db_path)
    first = [(c.case_id, c.suite, c.payload, c.note) for c in all_cases]
    second = [(c.case_id, c.suite, c.payload, c.note) for c in again]
    assert first == second


# ---------------------------------------------------------------------------
# compare_case：正/负样例
# ---------------------------------------------------------------------------


def _payload(spec: dict, expected: dict, by_dim: bool, label: str = "t") -> dict:
    return {
        "label": label,
        "sql": "SELECT 1",
        "by_dim": by_dim,
        "spec": spec,
        "expected": expected,
    }


def test_compare_no_dim_within_tolerance_is_true():
    payload = _payload(
        {"metrics": ["gmv"], "dimensions": []},
        {"columns": ["gmv"], "rows": [[100.0]]},
        by_dim=False,
    )
    # 相对 1e-5 / 绝对 1e-4 之内。
    ok, detail = compare_case(["gmv"], [[100.0000004]], payload)
    assert ok is True
    assert detail["diffs"] == []
    assert set(detail) == {"label", "sql", "expected", "actual", "diffs"}


def test_compare_no_dim_out_of_tolerance_is_false():
    payload = _payload(
        {"metrics": ["gmv"], "dimensions": []},
        {"columns": ["gmv"], "rows": [[100.0]]},
        by_dim=False,
    )
    ok, detail = compare_case(["gmv"], [[101.0]], payload)
    assert ok is False
    assert any("gmv" in d for d in detail["diffs"])


def test_compare_missing_column_is_false():
    payload = _payload(
        {"metrics": ["gmv", "aov"], "dimensions": []},
        {"columns": ["gmv", "aov"], "rows": [[100.0, 2.0]]},
        by_dim=False,
    )
    ok, detail = compare_case(["gmv"], [[100.0]], payload)
    assert ok is False
    assert any("缺列" in d and "aov" in d for d in detail["diffs"])


def test_compare_no_dim_wrong_row_count_is_false():
    payload = _payload(
        {"metrics": ["gmv"], "dimensions": []},
        {"columns": ["gmv"], "rows": [[100.0]]},
        by_dim=False,
    )
    ok, detail = compare_case(["gmv"], [[100.0], [200.0]], payload)
    assert ok is False
    assert any("1 行" in d for d in detail["diffs"])


def test_compare_by_dim_ignores_row_order():
    payload = _payload(
        {"metrics": ["gmv"], "dimensions": ["style"]},
        {"columns": ["style", "gmv"], "rows": [["A", 10.0], ["B", 20.0]]},
        by_dim=True,
    )
    ok, detail = compare_case(["style", "gmv"], [["B", 20.0], ["A", 10.0000001]], payload)
    assert ok is True
    assert detail["diffs"] == []


def test_compare_by_dim_missing_value_is_false():
    payload = _payload(
        {"metrics": ["gmv"], "dimensions": ["style"]},
        {"columns": ["style", "gmv"], "rows": [["A", 10.0], ["B", 20.0]]},
        by_dim=True,
    )
    ok, detail = compare_case(["style", "gmv"], [["A", 10.0]], payload)
    assert ok is False
    assert any("缺维度值" in d for d in detail["diffs"])
    assert any("行数不一致" in d for d in detail["diffs"])


# ---------------------------------------------------------------------------
# run()：小库上全对
# ---------------------------------------------------------------------------


def test_run_all_cases_pass_on_fixture_db(db_path, all_cases):
    result = run(db_path)
    assert isinstance(result, SuiteResult)
    assert result.suite == SUITE
    assert result.total == 20
    assert result.passed == 20
    assert result.accuracy == 1.0
    assert result.elapsed_ms > 0
    assert result.error is None
    assert result.meta == {"engine": "sql-truth-v1", "db": str(db_path)}
    assert result.failures() == []

    expected_ids = [c.case_id for c in all_cases]
    assert [r.case_id for r in result.cases] == expected_ids
    for case_result in result.cases:
        assert case_result.ok is True
        assert case_result.score == 1.0
        assert set(case_result.detail) == {"label", "sql", "expected", "actual", "diffs"}
        assert case_result.detail["diffs"] == []
        assert case_result.detail["label"]
        assert case_result.detail["sql"].strip()
        assert case_result.detail["expected"]["rows"]
        assert case_result.detail["actual"]["rows"]


def test_run_limit_returns_prefix(db_path):
    result = run(db_path, limit=3)
    assert result.total == 3
    assert result.passed == 3
    assert [case.case_id for case in result.cases] == ["Q01", "Q02", "Q03"]
    assert result.elapsed_ms > 0
