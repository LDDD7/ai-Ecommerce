"""检测层的促销口径 + 退款结构区分（B 轮修复）。

三件事必须钉住，否则会悄悄退回旧行为：
1. 促销降权阈值是 0.35（0.15 时「有一天大促」就把全指标压掉，实测把 10-20 的
   真实退款异常一起压没了）；
2. `match_promo` 是**能力**而不是默认——默认关闭，打开后 `promo_match` 要如实
   报告 matched/fallback，且匹配一定按「天集合」算而不是按 min~max 跨度；
3. `refund_split` 只做一件事：总退款率涨、尺码类不涨 → 更像 R10（物流）；
   尺码类同步涨 → 更像 R4（描述不符）。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from app.diagnose import hypothesis as hyp
from app.diagnose.anomaly import (
    PROMO_SKEW_THRESHOLD,
    DetectConfig,
    _matched_days,
    _promo_share_days,
    detect,
)

CUR = (date(2026, 10, 21), date(2026, 10, 27))
BASE = (date(2026, 10, 14), date(2026, 10, 20))


def test_promo_threshold_is_relaxed():
    """0.35 是有依据的：7 天窗里一天大促 = 0.14 的占比变化，不该触发全局降权。"""
    assert PROMO_SKEW_THRESHOLD == pytest.approx(0.35)


def test_matched_days_is_set_based():
    """按集合匹配：跨度里夹着的非促销天必须被剔掉。"""
    days = [date(2026, 10, d) for d in range(1, 11)]
    promo = {date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 9)}

    assert _matched_days(days, promo, want_promo=True) == [
        date(2026, 10, 1),
        date(2026, 10, 5),
        date(2026, 10, 9),
    ]
    assert _matched_days(days, promo, want_promo=False) == [
        date(2026, 10, d) for d in (2, 3, 4, 6, 7, 8, 10)
    ]


def test_promo_share_days_counts_days_not_span(db_path):
    """用 min~max 跨度算占比会把中间的非促销天算进去，这里必须按天算。"""
    conn = sqlite3.connect(db_path)
    promo = [r[0] for r in conn.execute("SELECT dt FROM dim_date WHERE is_promo = 1 LIMIT 2")]
    normal = [r[0] for r in conn.execute("SELECT dt FROM dim_date WHERE is_promo = 0 LIMIT 2")]
    conn.close()
    assert promo and normal

    assert _promo_share_days(db_path, {date.fromisoformat(promo[0])}) == pytest.approx(1.0)
    assert _promo_share_days(db_path, {date.fromisoformat(normal[0])}) == pytest.approx(0.0)
    two = {date.fromisoformat(promo[0]), date.fromisoformat(normal[0])}
    assert _promo_share_days(db_path, two) == pytest.approx(0.5)
    assert _promo_share_days(db_path, set()) == pytest.approx(0.0)


def test_detect_reports_promo_meta(db_path):
    """默认不匹配促销，但 meta 必须把这件事说清楚。"""
    cfg = DetectConfig(window_days=7, baseline_days=14)
    res = detect(db_path=db_path, as_of="2026-11-17", config=cfg)

    match = res.meta["promo_match"]
    assert match["enabled"] is False
    assert match["matched"] is False
    assert isinstance(match["window_days"], int)
    assert "promo_dominated" in res.meta


def test_detect_promo_match_enabled_reports_state(db_path):
    """打开匹配后，要么真的匹配上（matched=True），要么给出 fallback 原因。"""
    cfg = DetectConfig(window_days=7, baseline_days=14, match_promo=True, min_matched_days=4)
    res = detect(db_path=db_path, as_of="2026-11-17", config=cfg)

    match = res.meta["promo_match"]
    assert match["enabled"] is True
    assert match["matched"] or match["fallback"]


def _patch_query(monkeypatch, cur_refund, cur_return, base_refund, base_return, *, value="SKU0001"):
    """把 overview._query 换成固定两期数据（调用顺序固定：当期先、基期后）。"""
    calls = {"n": 0}

    def fake(db_path, metrics, start, end, dims=None, limit=5000):
        cols = (["sku_id"] if dims else []) + ["refund_rate", "return_rate"]
        n = calls["n"]
        calls["n"] += 1
        r, t = (cur_refund, cur_return) if n == 0 else (base_refund, base_return)
        row = [value, r, t] if dims else [r, t]
        return cols, [row]

    import app.insights.overview as ov

    monkeypatch.setattr(ov, "_query", fake)


def test_refund_split_logistics(monkeypatch):
    """总退款率 +154%、尺码类几乎不动 → 判 R10。"""
    _patch_query(monkeypatch, 0.30, 0.010, 0.118, 0.011)
    split = hyp.refund_split("x.db", "sku_id", "SKU0001", CUR, BASE)
    assert split is not None
    assert split["verdict"] == "R10"
    assert split["refund_rel"] > 0.15
    assert split["return_rel"] < 0.10


def test_refund_split_size(monkeypatch):
    """尺码类退货率同步大涨 → 判 R4。"""
    _patch_query(monkeypatch, 0.18, 0.05, 0.118, 0.012)
    split = hyp.refund_split("x.db", "sku_id", "SKU0001", CUR, BASE)
    assert split is not None
    assert split["verdict"] == "R4"


def test_refund_split_neutral(monkeypatch):
    """两者都不动/都动一点 → 不下结论（None），别硬猜。"""
    _patch_query(monkeypatch, 0.125, 0.013, 0.118, 0.012)
    split = hyp.refund_split("x.db", "sku_id", "SKU0001", CUR, BASE)
    assert split is not None
    assert split["verdict"] is None


def test_score_prefers_matching_refund_structure():
    """R10 结构成立时，R10 的置信度必须压过 R4，反之亦然。"""
    r10 = hyp.rank("refund_rate", z=3.0, support=2, split={"verdict": "R10"})
    r4 = hyp.rank("refund_rate", z=3.0, support=2, split={"verdict": "R4"})

    assert r10[0].code == "R10"
    assert r4[0].code == "R4"
    assert "refund_structure" in r10[0].promoted


def test_score_penalises_against_structure():
    """结构指向 R10 时，R4 要被扣分并留下痕迹。"""
    plain = hyp.score(hyp.Candidate(code="R4", title="尺码与描述不符", base_conf=0.70), z=3.0, support=1)
    hit = hyp.score(
        hyp.Candidate(code="R4", title="尺码与描述不符", base_conf=0.70),
        z=3.0,
        support=1,
        split={"verdict": "R10"},
    )
    assert hit.confidence < plain.confidence
    assert "refund_structure_against" in hit.promoted
    assert 0.2 <= hit.confidence <= 0.95
