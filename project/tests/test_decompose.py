"""恒等式分解的测试：残差、份额、口径一致性。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.diagnose.decompose import (
    Decomposition,
    decompose_orders,
    decompose_product,
    describe,
    gmv_decompose,
)


def test_no_regression_is_all_zero() -> None:
    dec = decompose_product(
        {"uv": 1000.0, "cvr": 0.02, "aov": 200.0},
        {"uv": 1000.0, "cvr": 0.02, "aov": 200.0},
        ("uv", "cvr", "aov"),
    )
    assert dec.delta == pytest.approx(0.0)
    assert dec.log_total == pytest.approx(0.0)
    assert dec.residual_money == pytest.approx(0.0)
    for f in dec.factors:
        assert f.rel == pytest.approx(0.0)
        assert f.log_contrib == pytest.approx(0.0)


def test_chain_decomposition_has_zero_residual() -> None:
    base = {"uv": 10_000.0, "cvr": 0.03, "aov": 180.0}
    cur = {"uv": 8_000.0, "cvr": 0.025, "aov": 200.0}
    dec = decompose_product(base, cur, ("uv", "cvr", "aov"))

    assert dec.total_base == pytest.approx(10_000 * 0.03 * 180)
    assert dec.total_cur == pytest.approx(8_000 * 0.025 * 200)
    assert dec.residual_money == pytest.approx(0.0, abs=1e-6)
    assert sum(f.money_contrib for f in dec.factors) == pytest.approx(dec.delta, abs=1e-6)


def test_log_shares_sum_to_one() -> None:
    dec = decompose_product(
        {"uv": 10_000.0, "cvr": 0.03, "aov": 180.0},
        {"uv": 8_000.0, "cvr": 0.025, "aov": 200.0},
        ("uv", "cvr", "aov"),
    )
    total = sum(f.log_share for f in dec.factors if f.log_share is not None)
    assert total == pytest.approx(1.0, abs=1e-9)
    # 流量跌得最多 → 排第一
    assert dec.top(1)[0].name == "uv"


def test_real_total_overrides_product_and_reports_gap() -> None:
    base = {"uv": 1000.0, "cvr": 0.05, "aov": 100.0}
    cur = {"uv": 1200.0, "cvr": 0.05, "aov": 100.0}
    dec = decompose_product(base, cur, ("uv", "cvr", "aov"), total_base=5000.0, total_cur=6000.0)
    assert dec.total_cur == pytest.approx(6000.0)
    assert dec.delta == pytest.approx(1000.0)
    # 因子连乘 6000 与真实 6000 一致 → gap 0
    assert dec.identity_gap_pct == pytest.approx(0.0)
    # 分量之和仍然等于真实总增量（残差只来自总量覆盖，会显式报出）
    assert dec.residual_money == pytest.approx(1000.0 - sum(f.money_contrib for f in dec.factors))


def test_zero_baseline_does_not_crash() -> None:
    dec = decompose_product(
        {"uv": 0.0, "cvr": 0.0, "aov": 0.0},
        {"uv": 100.0, "cvr": 0.02, "aov": 150.0},
        ("uv", "cvr", "aov"),
    )
    assert dec.total_base == 0.0
    assert dec.factors[0].log_contrib is None
    assert dec.delta_pct is None


def test_describe_is_chinese_and_mentions_money() -> None:
    dec = decompose_product(
        {"uv": 10_000.0, "cvr": 0.03, "aov": 180.0},
        {"uv": 8_000.0, "cvr": 0.025, "aov": 200.0},
        ("uv", "cvr", "aov"),
    )
    text = describe(dec)
    assert "成交额" in text
    assert "流量" in text and "转化率" in text


# ---------------- 真库集成（conftest 的小库） ----------------


def _windows(db_path) -> tuple[tuple[date, date], tuple[date, date]]:
    as_of = date(2026, 11, 18)
    cur = (as_of - timedelta(days=6), as_of)
    base = (cur[0] - timedelta(days=7), cur[0] - timedelta(days=1))
    return cur, base


def test_gmv_decompose_on_db(db_path) -> None:
    cur, base = _windows(db_path)
    dec = gmv_decompose(db_path, cur, base)
    assert isinstance(dec, Decomposition)
    assert dec.total_cur is not None and dec.total_cur > 0
    # 库里的比率是 6 位小数，残差只来自舍入；相对总变化应可忽略
    assert abs(dec.residual_money) <= max(0.01, 1e-4 * abs(dec.delta or 0.0))
    # 因子连乘应与订单视图 GMV 同口径（舍入差 < 1%）
    assert dec.identity_gap_pct is not None
    assert abs(dec.identity_gap_pct) < 0.01
    assert dec.factors[1].display == "转化率"


def test_decompose_orders_on_db(db_path) -> None:
    cur, base = _windows(db_path)
    dec = decompose_orders(db_path, cur, base)
    assert [f.name for f in dec.factors] == ["uv", "cvr"]
    assert abs(dec.residual_money) <= max(0.01, 1e-4 * abs(dec.delta or 0.0))
