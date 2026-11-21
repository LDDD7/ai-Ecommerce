"""异常检测的单元 + 集成测试。

单元部分不起数据库（构造序列直接调 _cell_change）；
集成部分用 conftest 的 20 SKU/30 天小库。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.diagnose import Window, detect, summarize
from app.diagnose.anomaly import DetectConfig, _cell_change, _stats, _severity


def _series(values: list[float], start: date) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_window_math():
    w = Window(date(2026, 11, 1), date(2026, 11, 7))
    assert w.days == 7
    assert w.contains(date(2026, 11, 1)) and w.contains(date(2026, 11, 7))
    assert not w.contains(date(2026, 10, 31))
    assert not w.contains(date(2026, 11, 8))


def test_pooled_proportion_detects_drop():
    """cvr 从 5% 掉到 3%，分母各 28000 / 5600 次详情 UV —— 应当显著。"""
    base = Window(date(2026, 10, 1), date(2026, 10, 28))
    cur = Window(date(2026, 10, 29), date(2026, 11, 4))
    cfg = DetectConfig()
    per_metric = {
        "cvr": _series([0.05] * 28, base.start) + _series([0.03] * 7, cur.start),
        "detail_uv": _series([1000.0] * 28, base.start) + _series([800.0] * 7, cur.start),
    }
    out = _cell_change("cvr", "ratio", per_metric, base, cur, cfg)
    assert out is not None
    p_cur, p_base, delta, z, n_cur, n_base = out
    assert p_base == pytest.approx(0.05, abs=1e-6)
    assert p_cur == pytest.approx(0.03, abs=1e-6)
    assert delta == pytest.approx(-0.02, abs=1e-6)
    assert z < -3.0
    assert (n_cur, n_base) == (7, 28)


def test_pooled_proportion_ignores_tiny_denominator():
    """分母只有 10/9 次时退回均值检验，不应给出极端 z。"""
    base = Window(date(2026, 10, 1), date(2026, 10, 28))
    cur = Window(date(2026, 10, 29), date(2026, 11, 4))
    cfg = DetectConfig()
    per_metric = {
        "refund_rate": _series([0.1] * 28, base.start) + _series([0.3] * 7, cur.start),
        "paid_orders": _series([10.0] * 28, base.start) + _series([9.0] * 7, cur.start),
    }
    out = _cell_change("refund_rate", "ratio", per_metric, base, cur, cfg)
    assert out is not None
    assert abs(out[3]) < 10.0  # 没有退化成 99 的伪显著


def test_cell_change_skips_short_windows():
    base = Window(date(2026, 10, 1), date(2026, 10, 28))
    cur = Window(date(2026, 11, 1), date(2026, 11, 4))
    cfg = DetectConfig()
    per_metric = {
        "gmv": _series([100.0] * 28, base.start) + _series([10.0] * 4, cur.start),
    }
    assert _cell_change("gmv", "currency", per_metric, base, cur, cfg) is None


def test_stats_poisson_floor():
    """基线零方差时，泊松下限保证 z 不会变成无穷大。"""
    _, _, delta, z = _stats("impressions", "count", [100.0] * 28, [130.0] * 7)
    assert delta == pytest.approx(30.0)
    assert 0 < z < 50


def test_severity_bands():
    assert _severity(-0.6, -5.0) == "high"
    assert _severity(-0.35, -3.2) == "medium"
    assert _severity(-0.16, -2.1) == "low"


def test_detect_on_small_db(db_path):
    r = detect(db_path)
    assert r.as_of == date(2026, 11, 18)
    assert r.window.days == 7
    assert r.baseline.end < r.window.start
    assert r.baseline.days == 28
    assert r.meta["engine"] == "zscore-v1"
    assert "promo_share" in r.meta
    assert r.meta["cells_scanned"] > 0

    for a, b in zip(r.points, r.points[1:]):
        assert a.score >= b.score
    for p in r.points:
        assert abs(p.delta_pct) >= r.meta["config"]["min_rel"]
        assert p.n_cur >= 4
        d = p.as_dict()
        assert d["window"]["days"] == 7
        assert d["dimension"] in (None, "style", "channel_id", "sku_id")


def test_detect_is_deterministic(db_path):
    a = detect(db_path)
    b = detect(db_path)
    assert [p.as_dict() for p in a.points] == [p.as_dict() for p in b.points]


def test_summarize_mentions_window(db_path):
    r = detect(db_path)
    text = summarize(r, 3)
    assert "巡检" in text
    assert r.window.start.isoformat() in text
