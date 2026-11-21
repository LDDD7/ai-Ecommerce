"""体检台概览测试（规则版扫描）。"""

from __future__ import annotations

from app.insights import build_overview


def test_overview_shape(db_path):
    body = build_overview(db_path)
    assert body["as_of"]
    assert body["window"]["days"] == 7
    assert len(body["kpis"]) == 6
    names = {k["name"] for k in body["kpis"]}
    assert {"gmv", "paid_orders", "aov", "cvr", "refund_rate", "roi"} == names
    assert 0 <= body["health"]["score"] <= 100
    assert body["health"]["grade"] in {"A", "B", "C", "D"}
    assert set(body["health"]["parts"]) == {"traffic", "conversion", "reputation"}
    assert body["meta"]["engine"] == "rules-v0"


def test_overview_trend_and_styles(db_path):
    body = build_overview(db_path)
    assert 1 <= len(body["trend"]) <= 30
    assert all({"dt", "gmv", "paid_orders"} <= set(p) for p in body["trend"])
    assert len(body["by_style"]) >= 1
    gmvs = [s["gmv"] or 0 for s in body["by_style"]]
    assert gmvs == sorted(gmvs, reverse=True)


def test_overview_cards_are_well_formed(db_path):
    body = build_overview(db_path)
    for card in body["cards"]:
        assert card["code"].startswith("R")
        assert card["severity"] in {"high", "mid"}
        assert card["delta_pct"] <= -15
        assert card["impact_gmv"] >= 0
        assert card["source"] == "rules-v0"
    assert len(body["cards"]) <= 6


def test_overview_window_bounds(db_path):
    body = build_overview(db_path, window_days=14)
    assert body["window"]["days"] == 14
    assert body["window"]["start"] < body["window"]["end"]
