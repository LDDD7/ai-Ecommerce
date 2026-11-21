"""Critic 规则版的补充用例：覆盖对抗性测试发现的几处判定错误。"""
from __future__ import annotations

from app.diagnose.critic import apply_verdicts, critique


def _card(code: str, metric: str, conf: float = 0.80, **kw):
    card = {
        "card_id": kw.get("card_id", "card_001"),
        "code": code,
        "title": "测试卡",
        "primary_metric": metric,
        "confidence": conf,
        "scope": {"dim": kw.get("dim", "sku_id"), "dim_label": "商品", "dim_value": "SKU0001"},
        "evidence": kw.get("evidence", []),
        "gmv_impact": kw.get("gmv_impact", -20000.0),
        "impact_share": kw.get("impact_share", 0.2),
        "caveats": kw.get("caveats", []),
    }
    if "competitor_gap" in kw:
        card["competitor_gap"] = kw["competitor_gap"]
    return card


def _ev(metric: str, delta: float, z: float = -4.0) -> dict:
    return {"metric": metric, "delta": delta, "delta_pct": delta, "z": z}


def test_r7_without_promo_data_does_not_invent_zero():
    """promo_skew 缺失时不能伪造「+0.00」，要明说缺数据。"""
    card = _card(
        "R7",
        "impressions",
        evidence=[_ev("impressions", -0.35), _ev("paid_orders", -0.30)],
    )

    v = critique([card], promo_skew=None)[0]

    assert not any("+0.00" in o for o in v.objections)
    assert any("缺少促销日占比数据" in o for o in v.objections)
    # 缺数据比「确认没有促销」更轻，只打 0.85。
    assert abs(v.factor - 0.85) < 1e-9


def test_r3_stockout_not_penalized_by_promo_window():
    """R3 断货与促销无因果，促销偏斜不该压它。"""
    card = _card(
        "R3",
        "paid_orders",
        evidence=[_ev("paid_orders", -0.40), _ev("stockout_rate", 0.60, z=5.0)],
    )

    v = critique([card], promo_skew=0.45)[0]

    promo = next(c for c in v.checks if c.name == "promo")
    assert promo.ok is True
    assert v.factor == 1.0


def test_rejected_card_does_not_block_next_of_same_root():
    """被驳回的卡已经不在报告里，不该再占同根因名额。"""
    first = _card("R3", "paid_orders", conf=0.30, card_id="card_001",
                  evidence=[_ev("paid_orders", 0.40)])
    second = _card("R3", "paid_orders", conf=0.60, card_id="card_002",
                   evidence=[_ev("paid_orders", -0.40), _ev("stockout_rate", 0.60, z=5.0)])

    verdicts = critique([first, second])

    assert verdicts[0].verdict == "reject"
    dup = next(c for c in verdicts[1].checks if c.name == "duplicate")
    assert dup.ok is True


def test_apply_verdicts_is_idempotent():
    """重复调用不重复追加「反方：」条目。"""
    card = _card("R1", "cvr", conf=0.68, competitor_gap=0.01,
                 evidence=[_ev("cvr", -0.20), _ev("gmv", -0.18)],
                 caveats=["我方售价比竞品最低价高 1.0%，需关注"])
    verdicts = critique([card], promo_skew=0.0)

    kept, _ = apply_verdicts([card], verdicts)
    first = list(kept[0]["caveats"])
    kept, _ = apply_verdicts([card], verdicts)

    assert kept[0]["caveats"] == first
    # 卡片自带「竞品最低价」提示，反方同一话题不再重复（去重命中），也不会重复追加上一次的结果。
    assert sum(1 for c in kept[0]["caveats"] if c.startswith("反方：")) == 0


def test_market_check_ignores_non_gmv_metrics():
    """客单价与大盘 GMV 不同量纲，不该触发「随大盘」。"""
    card = _card("R9", "unit_price", evidence=[_ev("unit_price", 0.03, z=4.0)])

    v = critique([card], market_delta_pct=0.02)[0]

    market = next(c for c in v.checks if c.name == "market")
    assert market.ok is True
