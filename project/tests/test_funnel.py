"""漏斗分析的测试：整体转化率 = 各级连乘，log 贡献可加。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.diagnose.funnel import STEPS, describe, funnel, funnel_by_dim


def _windows() -> tuple[tuple[date, date], tuple[date, date]]:
    as_of = date(2026, 11, 18)
    cur = (as_of - timedelta(days=6), as_of)
    base = (cur[0] - timedelta(days=7), cur[0] - timedelta(days=1))
    return cur, base


def test_step_order_and_overall_rate(db_path) -> None:
    cur, base = _windows()
    f = funnel(db_path, cur, base)
    assert [s.name for s in f.steps] == [name for name, _ in STEPS]
    # 整体转化率 = 支付 / 曝光
    assert f.overall_cur == pytest.approx(f.steps[-1].cur / f.steps[0].cur)
    assert f.overall_base == pytest.approx(f.steps[-1].base / f.steps[0].base)


def test_step_rates_multiply_to_overall(db_path) -> None:
    cur, base = _windows()
    f = funnel(db_path, cur, base)
    prod = 1.0
    for s in f.steps[1:]:
        prod *= s.rate_cur
    assert prod == pytest.approx(f.overall_cur)


def test_log_contributions_are_additive(db_path) -> None:
    cur, base = _windows()
    f = funnel(db_path, cur, base)
    total = sum(s.log_contrib for s in f.steps[1:] if s.log_contrib is not None)
    assert total == pytest.approx(f.log_total, abs=1e-9)
    assert f.residual == pytest.approx(0.0, abs=1e-9)


def test_shares_sum_to_one_when_total_nonzero(db_path) -> None:
    cur, base = _windows()
    f = funnel(db_path, cur, base)
    if f.log_total and abs(f.log_total) > 1e-9:
        assert sum(s.share for s in f.steps if s.share is not None) == pytest.approx(1.0, abs=1e-9)


def test_worst_step_is_most_negative(db_path) -> None:
    cur, base = _windows()
    f = funnel(db_path, cur, base)
    worst = f.worst_step()
    if worst is not None:
        assert all(worst.log_contrib <= (s.log_contrib or math.inf) for s in f.steps)

    text = describe(f)
    assert "曝光→支付" in text


def test_funnel_by_dim_returns_sorted_list(db_path) -> None:
    cur, base = _windows()
    rows = funnel_by_dim(db_path, cur, base, "style", limit=3)
    assert 1 <= len(rows) <= 3
    exposures = [r.steps[0].base for r in rows]
    assert exposures == sorted(exposures, reverse=True)
