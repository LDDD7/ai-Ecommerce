"""规则版 Critic 的对抗性测试。

目标不是重复验证「正常卡能通过」，而是逐条钉住 critic 可能误判的典型场景：
方向反了、上游更可疑、促销口径不可比、辅证缺失、证据太弱、影响太小、
同根因重复，以及 apply_verdicts 的写回与驳回。

约定：
- 卡片就是 DiagnosisCard.as_dict() 的普通 dict，不依赖数据库；
- 每个用例只构造必要字段，避免让无关规则意外参与；
- 断言用精确的折扣系数与 verdict 边界，防止规则被悄悄放松。
"""

from __future__ import annotations

import copy
import json

import pytest

from app.diagnose.critic import (
    apply_verdicts,
    critique,
    describe,
)
from app.diagnose.hypothesis import CODE_TITLES


def _ev(
    metric: str,
    delta: float,
    delta_pct: float,
    z: float,
    *,
    dim_field: str = "style",
    dim_value: str = "甜美",
    unit: str = "%",
) -> dict:
    """按 DiagnosisCard.evidence 的字段造一条证据。"""
    return {
        "metric": metric,
        "display": metric,
        "dim_field": dim_field,
        "dim_value": dim_value,
        "window": {"start": "2026-11-01", "end": "2026-11-07"},
        "baseline": 100.0,
        "current": 100.0 * (1.0 + delta_pct),
        "delta": delta,
        "delta_pct": delta_pct,
        "z": z,
        "contribution_pct": delta_pct,
        "unit": unit,
    }


def _card(
    card_id: str,
    code: str,
    *,
    primary_metric: str,
    evidence: list[dict],
    confidence: float = 0.8,
    gmv_impact: float | None = -60000.0,
    impact_share: float | None = -0.2,
    caveats: list[str] | None = None,
    scope_dim: str = "style",
    **extra: object,
) -> dict:
    """造一张与 DiagnosisCard.as_dict() 同形的卡片，默认参数让无关检查通过。"""
    title = CODE_TITLES.get(code, code)
    card = {
        "card_id": card_id,
        "code": code,
        "title": title,
        "headline": f"{title} 测试卡",
        "scope": {"dim": scope_dim, "dim_label": "款式", "dim_value": "甜美"},
        "severity": "high",
        "confidence": confidence,
        "primary_metric": primary_metric,
        "window": {"start": "2026-11-01", "end": "2026-11-07"},
        "baseline_window": {"start": "2026-10-25", "end": "2026-10-31"},
        "evidence": evidence,
        "gmv_impact": gmv_impact,
        "impact_share": impact_share,
        "narrative": "测试卡片。",
        "caveats": list(caveats or []),
        "candidates": [],
    }
    card.update(extra)
    return card


def _check(verdict, name: str):
    """按名字取 check，避免用例依赖 checks 的下标顺序。"""
    for check in verdict.checks:
        if check.name == name:
            return check
    raise AssertionError(f"没有名为 {name} 的 check：{[c.name for c in verdict.checks]}")


def test_方向不符_R2主指标上涨应打折():
    """误判：ctr 在涨，却被归因「主图素材衰减」。

    只校验「有异常」而不校验方向，会把因果相反的卡当成成立。
    """
    verdict = critique([
        _card(
            "c1",
            "R2",
            primary_metric="ctr",
            evidence=[
                _ev("ctr", 0.004, 0.08, 3.2),
                _ev("clicks", -120, -0.04, -2.2),
            ],
        )
    ])[0]

    direction = _check(verdict, "direction")
    assert direction.ok is False
    assert "本应下降" in direction.detail
    assert any("本应下降" in objection for objection in verdict.objections)
    assert verdict.confidence_after == pytest.approx(0.8 * 0.45)
    assert verdict.verdict == "weaken"


def test_上游更可能_曝光跌幅不亚于点击率():
    """误判：把「流量结构变化」写成「主图衰减」。

    曝光跌幅和点击率跌幅一样大时，上游供给/流量解释比主图更可疑。
    """
    verdict = critique([
        _card(
            "c1",
            "R2",
            primary_metric="ctr",
            evidence=[
                _ev("ctr", -0.01, -0.20, -4.0),
                _ev("impressions", -1500, -0.30, -5.0),
            ],
        )
    ])[0]

    upstream = _check(verdict, "upstream")
    assert upstream.ok is False
    assert "曝光" in upstream.detail
    assert any("曝光" in objection for objection in verdict.objections)
    assert verdict.confidence_after == pytest.approx(0.8 * 0.70)


def test_反向_R8点击率跌幅大于曝光():
    """误判：曝光只跌 5%、点击率跌 25%，却归因「竞争加剧/流量下滑」。

    这种形状更像素材或相关性下降，R8 的竞争解释应被质疑。
    """
    verdict = critique([
        _card(
            "c1",
            "R8",
            primary_metric="impressions",
            evidence=[
                _ev("impressions", -200, -0.05, -2.6),
                _ev("ctr", -0.012, -0.25, -4.5),
            ],
        )
    ])[0]

    upstream = _check(verdict, "upstream")
    assert upstream.ok is False
    assert "点击率跌幅" in upstream.detail
    assert any("点击率跌幅" in objection for objection in verdict.objections)
    assert verdict.confidence_after == pytest.approx(0.8 * 0.70)


def test_R7缺促销证据_促销日占比不变():
    """误判：日常波动被写成「大促后透支」。

    promo_skew 为 0 时没有任何促销透支证据，R7 不应直接成立。
    """
    verdict = critique(
        [
            _card(
                "c1",
                "R7",
                primary_metric="impressions",
                evidence=[
                    _ev("impressions", -1200, -0.35, -5.0),
                    _ev("paid_orders", -80, -0.30, -4.0),
                ],
            )
        ],
        promo_skew=0.0,
    )[0]

    upstream = _check(verdict, "upstream")
    assert upstream.ok is False
    assert "促销日占比变化" in upstream.detail
    assert any("促销日占比变化" in objection for objection in verdict.objections)
    assert verdict.factor == pytest.approx(0.70)


def test_R4缺辅证_没有退货率():
    """误判：只有退款率上升就断定「尺码与描述不符」。

    退款率是主症状，return_rate 是辅证；缺辅证时结论偏薄。
    """
    verdict = critique([
        _card(
            "c1",
            "R4",
            primary_metric="refund_rate",
            evidence=[
                _ev("refund_rate", 0.02, 0.50, 4.0),
                _ev("gmv", -8000, -0.12, -3.0),
            ],
        )
    ])[0]

    upstream = _check(verdict, "upstream")
    assert upstream.ok is False
    assert "缺少辅证" in upstream.detail
    assert "return_rate" in upstream.detail
    assert any("缺少辅证" in objection for objection in verdict.objections)
    assert verdict.confidence_after == pytest.approx(0.8 * 0.80)


def test_R1竞品价_缺失与价差过小都该被质疑():
    """误判：把所有转化下滑都归为「竞品降价战」。

    没有竞品价是没证据；只比竞品高 1% 也解释不了两位数的转化下滑。
    """
    evidence = [
        _ev("cvr", -0.006, -0.18, -4.0),
        _ev("paid_orders", -90, -0.15, -3.2),
    ]
    no_gap = critique([
        _card("c1", "R1", primary_metric="cvr", evidence=evidence, confidence=0.68)
    ])[0]
    tiny_gap = critique([
        _card(
            "c1",
            "R1",
            primary_metric="cvr",
            evidence=evidence,
            confidence=0.68,
            competitor_gap=0.01,
        )
    ])[0]

    no_gap_check = _check(no_gap, "upstream")
    assert no_gap_check.ok is False
    assert "竞品价格对比" in no_gap_check.detail
    assert no_gap.verdict == "support"
    assert no_gap.confidence_after == pytest.approx(0.68 * 0.85)

    tiny_gap_check = _check(tiny_gap, "upstream")
    assert tiny_gap_check.ok is False
    assert tiny_gap_check.weight == pytest.approx(0.75)
    assert "1.0%" in tiny_gap_check.detail
    assert tiny_gap.verdict == "weaken"
    assert no_gap.confidence_after > tiny_gap.confidence_after


def test_证据太弱_z值全部低于阈值():
    """误判：三条 z≈1 的正常波动拼成一张卡。

    最强证据都不显著时，不应该当成需要处理的异常。
    """
    verdict = critique([
        _card(
            "c1",
            "R2",
            primary_metric="ctr",
            evidence=[
                _ev("ctr", -0.002, -0.04, -1.2),
                _ev("clicks", -40, -0.03, -0.8),
                _ev("paid_orders", -20, -0.02, -0.5),
            ],
        )
    ])[0]

    sample = _check(verdict, "sample")
    assert sample.ok is False
    assert "最强证据 z=1.2" in sample.detail
    assert "低于 2.5" in sample.detail
    assert verdict.confidence_after == pytest.approx(0.8 * 0.80)


def test_证据太弱_只有一条证据():
    """误判：单条强异常就下结论，缺少第二条交叉印证。"""
    verdict = critique([
        _card(
            "c1",
            "R2",
            primary_metric="ctr",
            evidence=[_ev("ctr", -0.02, -0.35, -6.0)],
        )
    ])[0]

    sample = _check(verdict, "sample")
    assert sample.ok is False
    assert "只有一条证据" in sample.detail
    assert verdict.confidence_after == pytest.approx(0.8 * 0.90)


def test_促销口径_按主指标生效_R2量价卡挂_R7豁免():
    """误判：大促周的量价不可比，却被当成独立证据。

    当前实现按 primary_metric 判断，而不是按 code 一刀切：
    - R2 + ctr：ctr 不在 PROMO_SENSITIVE，不挂；
    - R2 + paid_orders：量价指标，promo 挂 0.65；
    - R7：促销本身就是根因，豁免 promo 检查。

    注意：任务书原设想「promo_skew=0.35 且 code=R2 就挂」，与当前
    critic.py 的 PROMO_SENSITIVE 收窄不一致；这里钉住当前真实行为，
    差异已写进最终报告，不通过放宽断言来掩盖。
    """
    ctr_card = _card(
        "c1",
        "R2",
        primary_metric="ctr",
        evidence=[
            _ev("ctr", -0.01, -0.20, -4.0),
            _ev("clicks", -100, -0.18, -3.5),
        ],
    )
    paid_card = _card(
        "c2",
        "R2",
        primary_metric="paid_orders",
        evidence=[
            _ev("paid_orders", -100, -0.30, -4.0),
            _ev("ctr", -0.01, -0.20, -3.5),
        ],
    )
    r7_card = _card(
        "c3",
        "R7",
        primary_metric="impressions",
        evidence=[
            _ev("impressions", -1000, -0.30, -5.0),
            _ev("paid_orders", -60, -0.25, -3.5),
        ],
    )

    ctr_verdict = critique([ctr_card], promo_skew=0.35)[0]
    assert _check(ctr_verdict, "promo").ok is True
    assert ctr_verdict.factor == pytest.approx(1.0)
    assert ctr_verdict.verdict == "support"

    paid_verdict = critique([paid_card], promo_skew=0.35)[0]
    assert _check(paid_verdict, "promo").ok is False
    assert "量价对比不可比" in _check(paid_verdict, "promo").detail
    assert paid_verdict.confidence_after == pytest.approx(0.8 * 0.65)
    assert paid_verdict.verdict == "weaken"

    r7_verdict = critique([r7_card], promo_skew=0.35)[0]
    assert _check(r7_verdict, "promo").ok is True
    assert r7_verdict.factor == pytest.approx(1.0)
    assert r7_verdict.verdict == "support"


def test_促销口径_ROI量价卡受偏斜影响():
    """误判：大促周 ROI 下滑被当成投放效率问题。

    ROI 与花费在 PROMO_SENSITIVE 里，促销日占比偏斜超过 30% 时不可比。
    """
    verdict = critique(
        [
            _card(
                "c1",
                "R5",
                primary_metric="roi",
                evidence=[
                    _ev("roi", -0.4, -0.30, -4.0),
                    _ev("cpc", 0.5, 0.25, 3.5),
                ],
            )
        ],
        promo_skew=0.35,
    )[0]

    promo = _check(verdict, "promo")
    assert promo.ok is False
    assert "量价对比不可比" in promo.detail
    assert verdict.confidence_after == pytest.approx(0.8 * 0.65)
    assert verdict.verdict == "weaken"


def test_影响太小_金额不足或占比超过100都该降权():
    """误判：影响只有 120 元还排在前面；或占比 140% 这种口径错误没被发现。"""
    small = critique([
        _card(
            "c1",
            "R2",
            primary_metric="ctr",
            evidence=[
                _ev("ctr", -0.01, -0.20, -4.0),
                _ev("clicks", -100, -0.18, -3.5),
            ],
            gmv_impact=-120.0,
            impact_share=-0.001,
        )
    ])[0]
    share = critique([
        _card(
            "c2",
            "R2",
            primary_metric="ctr",
            evidence=[
                _ev("ctr", -0.01, -0.20, -4.0),
                _ev("clicks", -100, -0.18, -3.5),
            ],
            gmv_impact=-60000.0,
            impact_share=1.4,
        )
    ])[0]

    assert _check(small, "impact").ok is False
    assert "优先级低" in _check(small, "impact").detail
    assert any("优先级低" in objection for objection in small.objections)
    assert small.confidence_after == pytest.approx(0.8 * 0.80)

    assert _check(share, "impact").ok is False
    assert "口径可疑" in _check(share, "impact").detail
    assert any("口径可疑" in objection for objection in share.objections)
    assert share.confidence_after == pytest.approx(0.8 * 0.85)


def test_同根因重复_第二张降权():
    """误判：两张 R3 讲同一件事，第二张仍按独立证据展示。"""
    def r3_card(card_id: str, confidence: float) -> dict:
        return _card(
            card_id,
            "R3",
            primary_metric="paid_orders",
            confidence=confidence,
            evidence=[
                _ev("paid_orders", -200, -0.40, -6.0),
                _ev("stockout_rate", 0.08, 0.60, 5.0),
            ],
        )

    verdicts = critique([r3_card("c1", 0.62), r3_card("c2", 0.62)])

    assert _check(verdicts[0], "duplicate").ok is True
    assert not any("同根因" in objection for objection in verdicts[0].objections)
    assert _check(verdicts[1], "duplicate").ok is False
    assert "同根因" in _check(verdicts[1], "duplicate").detail
    assert any("同根因" in objection for objection in verdicts[1].objections)
    assert verdicts[0].confidence_after > verdicts[1].confidence_after
    assert verdicts[0].verdict == "support"
    assert verdicts[1].verdict == "weaken"


def test_站得住的正例_R3断货():
    """反例校准：证据齐、方向对、z 够强、影响够大时，critic 不该误杀。"""
    verdict = critique([
        _card(
            "c1",
            "R3",
            primary_metric="paid_orders",
            confidence=0.82,
            evidence=[
                _ev("paid_orders", -260, -0.42, -6.0),
                _ev("stockout_rate", 0.09, 0.75, 5.5),
            ],
        )
    ])[0]

    assert verdict.verdict == "support"
    assert verdict.factor == pytest.approx(1.0)
    assert verdict.objections == []
    assert verdict.confidence_after >= verdict.confidence_before * 0.95


def test_apply_verdicts_驳回移出与保留改写():
    """误判防护：低置信度且被方向规则打穿的卡必须从展示列表消失；
    保留卡要改写置信度，并留下「反方：」痕迹。
    """
    reject_card = _card(
        "c1",
        "R2",
        primary_metric="ctr",
        confidence=0.30,
        evidence=[
            _ev("ctr", 0.004, 0.08, 3.2),
            _ev("clicks", -120, -0.04, -2.2),
        ],
    )
    keep_card = _card(
        "c2",
        "R1",
        primary_metric="cvr",
        confidence=0.80,
        evidence=[
            _ev("cvr", -0.006, -0.18, -4.0),
            _ev("paid_orders", -90, -0.15, -3.2),
        ],
        caveats=["历史遗留注意事项"],
    )

    verdicts = critique([reject_card, keep_card])
    kept, rejected = apply_verdicts([reject_card, keep_card], verdicts)

    assert [card["card_id"] for card in rejected] == ["c1"]
    assert [card["card_id"] for card in kept] == ["c2"]

    assert kept[0]["confidence"] == pytest.approx(0.68, abs=1e-3)
    assert kept[0]["critic"]["verdict"] == "support"
    assert kept[0]["caveats"][0] == "历史遗留注意事项"
    assert any(item.startswith("反方：") for item in kept[0]["caveats"])

    assert rejected[0]["confidence"] == pytest.approx(0.135, abs=1e-3)
    assert rejected[0]["critic"]["verdict"] == "reject"


def test_describe三种结论与JSON序列化():
    """报告层要消费：三种 verdict 都有中文词，Verdict.as_dict() 必须可 json.dumps。"""
    support_verdict = critique([
        _card(
            "s1",
            "R3",
            primary_metric="paid_orders",
            confidence=0.80,
            evidence=[
                _ev("paid_orders", -260, -0.42, -6.0),
                _ev("stockout_rate", 0.09, 0.75, 5.5),
            ],
        )
    ])[0]
    weaken_verdict = critique([
        _card(
            "w1",
            "R2",
            primary_metric="ctr",
            confidence=0.80,
            evidence=[
                _ev("ctr", 0.004, 0.08, 3.2),
                _ev("clicks", -120, -0.04, -2.2),
            ],
        )
    ])[0]
    reject_verdict = critique([
        _card(
            "r1",
            "R2",
            primary_metric="ctr",
            confidence=0.30,
            evidence=[
                _ev("ctr", 0.004, 0.08, 3.2),
                _ev("clicks", -120, -0.04, -2.2),
            ],
        )
    ])[0]

    assert "成立" in describe(support_verdict)
    assert "存疑" in describe(weaken_verdict)
    assert "驳回" in describe(reject_verdict)
    for verdict in (support_verdict, weaken_verdict, reject_verdict):
        assert "置信度" in describe(verdict)

    payload = [verdict.as_dict() for verdict in (support_verdict, weaken_verdict, reject_verdict)]
    text = json.dumps(payload, ensure_ascii=False)
    loaded = json.loads(text)
    assert [item["verdict"] for item in loaded] == ["support", "weaken", "reject"]


def test_确定性_同一输入两次调用完全一致():
    """critic 是纯函数：同样的卡与促销偏斜，两次输出必须逐字段相同，且不修改入参。"""
    cards = [
        _card(
            "c1",
            "R2",
            primary_metric="ctr",
            evidence=[
                _ev("ctr", -0.01, -0.20, -4.0),
                _ev("impressions", -1500, -0.30, -5.0),
            ],
        ),
        _card(
            "c2",
            "R3",
            primary_metric="paid_orders",
            confidence=0.62,
            evidence=[
                _ev("paid_orders", -200, -0.40, -6.0),
                _ev("stockout_rate", 0.08, 0.60, 5.0),
            ],
        ),
        _card(
            "c3",
            "R8",
            primary_metric="impressions",
            evidence=[
                _ev("impressions", -200, -0.05, -2.6),
                _ev("ctr", -0.012, -0.25, -4.5),
            ],
        ),
    ]
    snapshot = copy.deepcopy(cards)

    first = critique(cards, promo_skew=0.35, market_delta_pct=-0.02)
    second = critique(cards, promo_skew=0.35, market_delta_pct=-0.02)

    assert [verdict.as_dict() for verdict in first] == [
        verdict.as_dict() for verdict in second
    ]
    assert cards == snapshot
