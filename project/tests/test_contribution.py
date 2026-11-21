"""维度贡献度的测试：分量之和必须等于总变化。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.diagnose.contribution import (
    additive_contribution,
    approx_contribution,
    contribution,
    describe,
    ratio_contribution,
)


def _windows(db_path) -> tuple[tuple[date, date], tuple[date, date]]:
    as_of = date(2026, 11, 18)
    cur = (as_of - timedelta(days=6), as_of)
    base = (cur[0] - timedelta(days=7), cur[0] - timedelta(days=1))
    return cur, base


def test_additive_parts_sum_to_total(db_path) -> None:
    cur, base = _windows(db_path)
    res = additive_contribution(db_path, "gmv", "style", cur, base)
    assert res.method == "additive"
    assert res.residual == pytest.approx(0.0, abs=1e-6)
    assert sum(p.delta for p in res.parts) == pytest.approx(res.delta, abs=1e-6)
    # 份额相加为 1（总变化非 0 时）
    if res.delta and abs(res.delta) > 1e-9:
        assert sum(p.share for p in res.parts) == pytest.approx(1.0, abs=1e-9)
    # 按 |delta| 降序
    mags = [abs(p.delta) for p in res.parts]
    assert mags == sorted(mags, reverse=True)


def test_ratio_parts_sum_to_rate_delta(db_path) -> None:
    cur, base = _windows(db_path)
    res = ratio_contribution(db_path, "cvr", "channel_id", cur, base)
    assert res.method == "ratio"
    assert res.delta is not None
    assert res.residual == pytest.approx(0.0, abs=1e-12)
    assert sum(p.delta for p in res.parts) == pytest.approx(res.delta, abs=1e-12)


def test_top_merges_rest(db_path) -> None:
    cur, base = _windows(db_path)
    res = additive_contribution(db_path, "gmv", "sku_id", cur, base, top=3)
    assert len(res.parts) <= 4
    assert res.parts[-1].dim_value == "其他"
    # 合并后总和仍等于总变化
    assert sum(p.delta for p in res.parts) == pytest.approx(res.delta, abs=1e-6)


def test_dispatch_picks_ratio_for_cvr(db_path) -> None:
    cur, base = _windows(db_path)
    assert contribution(db_path, "cvr", "style", cur, base).method == "ratio"
    assert contribution(db_path, "gmv", "style", cur, base).method == "additive"
    # 退款率没有分子/分母定义 → 走近似
    assert approx_contribution(db_path, "refund_rate", "style", cur, base).method == "approx"


def test_describe_mentions_main_cause(db_path) -> None:
    cur, base = _windows(db_path)
    text = describe(additive_contribution(db_path, "gmv", "style", cur, base))
    assert "主因" in text
