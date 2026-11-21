"""证据链 / 根因假设 / 诊断卡 的测试。

原则：诊断卡是最终给人看的东西，所以这里不只验字段齐全，还验：
- 每张卡的证据都 `is_complete()`（spec §7.5 硬规则）；
- 卡按影响金额排序，且绝不出现「没有证据硬下结论」；
- 规则候选的排序在促销偏斜、竞品价差下会变，但不会越界。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.diagnose.diagnosis import MAX_EVIDENCE, build_diagnosis
from app.diagnose.evidence import Evidence, complete_only, is_complete, summarize
from app.diagnose.hypothesis import CODE_TITLES, candidates_for, competitor_gap, rank


def _point(window=("2026-11-01", "2026-11-07")) -> SimpleNamespace:
    win = None
    if window:
        win = SimpleNamespace(
            start=window[0],
            end=window[1],
            as_dict=lambda: {"start": window[0], "end": window[1]},
        )
    return SimpleNamespace(
        metric="ctr",
        display="点击率",
        dimension="style",
        dim_value="甜美",
        window=win,
        baseline=0.05,
        current=0.03,
        delta=-0.02,
        delta_pct=-0.4,
        z=-4.2,
        unit="%",
    )


def test_evidence_from_point_complete():
    ev = Evidence.from_point(_point(), contribution_pct=-0.12)
    assert isinstance(ev, Evidence)
    assert ev.key == "ctr@style=甜美"
    assert ev.window == {"start": "2026-11-01", "end": "2026-11-07"}
    assert ev.contribution_pct == -0.12
    assert is_complete(ev)
    assert complete_only([ev]) == [ev]
    assert "点击率" in summarize([ev])


def test_evidence_missing_window_is_incomplete():
    ev = Evidence.from_point(_point(window=None))
    assert not is_complete(ev)
    assert complete_only([ev]) == []


def test_candidates_prior():
    assert candidates_for("ctr")[0].code == "R2"
    assert candidates_for("roi")[0].code == "R5"
    assert candidates_for("stockout_rate")[0].code == "R3"
    assert candidates_for("no_such_metric") == []


def test_rank_confident_and_bounded():
    top = rank("cvr", z=-6.0, support=3, limit=3)
    assert top[0].code == "R1"
    assert all(0.2 <= c.confidence <= 0.95 for c in top)
    assert all(c.code in CODE_TITLES for c in top)


def test_rank_promo_window_promotes_r7():
    normal = rank("paid_orders", z=-3.0, support=2)
    promo = rank("paid_orders", z=-3.0, support=2, promo_skew=0.62)
    assert normal[0].code == "R3"
    assert promo[0].code == "R7"
    assert "promo_window" in promo[0].promoted


def test_competitor_gap_on_db(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT sku_id FROM fact_competitor_price LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        pytest.skip("测试库没有竞品价数据")
    from datetime import date

    gap = competitor_gap(db_path, row[0], (date(2026, 11, 1), date(2026, 11, 28)))
    assert gap is not None
    assert gap["competitor_min"] > 0
    assert isinstance(gap["gap"], float)


def test_build_diagnosis_shape(db_path):
    report = build_diagnosis(db_path, as_of="2026-11-10", window_days=7, max_cards=3)
    assert set(report) >= {"as_of", "window", "baseline_window", "cards", "gmv", "funnel", "meta"}
    assert report["as_of"] == "2026-11-10"
    assert report["meta"]["engine"] == "rules-v1"
    assert len(report["cards"]) <= 3

    for card in report["cards"]:
        assert card["code"] in CODE_TITLES
        assert card["title"] == CODE_TITLES[card["code"]]
        assert 1 <= len(card["evidence"]) <= MAX_EVIDENCE
        assert 0.0 <= card["confidence"] <= 1.0
        assert card["narrative"].endswith("。")
        assert card["scope"]["dim"] in {"style", "channel_id", "sku_id"}
        assert set(card["window"]) == {"start", "end"}

    assert "成交额" in report["gmv"]["summary"]
    assert "曝光" in report["funnel"]["summary"]


def test_cards_sorted_by_impact(db_path):
    report = build_diagnosis(db_path, as_of="2026-11-10", window_days=7, max_cards=5)
    impacts = [abs(c["gmv_impact"] or 0.0) for c in report["cards"]]
    assert impacts == sorted(impacts, reverse=True)


def test_diagnosis_never_claims_without_evidence(db_path):
    """证据不全时卡应被丢弃，而不是输出一句话。"""
    report = build_diagnosis(db_path, as_of="2026-11-10", window_days=7, max_cards=5)
    for card in report["cards"]:
        for ev in card["evidence"]:
            assert ev["window"].get("start") and ev["window"].get("end")
            assert ev["metric"] and ev["dim_field"] and ev["dim_value"]
            assert ev["delta"] is not None
    if not report["cards"]:
        assert report["meta"]["dropped"]
