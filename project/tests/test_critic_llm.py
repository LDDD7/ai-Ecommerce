"""LLM 反方审查测试：全部离线，注入假 LLM，不访问网络、不碰真库。"""

from __future__ import annotations

from app.core.llm import LLMError
from app.diagnose.critic import Check, Verdict, critique
from app.diagnose.critic_llm import review_cards


class FakeLLM:
    """最小的假 LLM：只实现 duck typing 需要的 chat_json。"""

    def __init__(self, body=None, exc: Exception | None = None):
        self.body = body
        self.exc = exc
        self.calls: list[tuple[list[dict], dict]] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.body


def _card(
    card_id: str = "card_001",
    code: str = "R2",
    title: str = "主图衰减",
    confidence: float = 0.70,
    n_evidence: int = 3,
) -> dict:
    """一张最小可用的诊断卡（字段与 DiagnosisCard.as_dict 一致）。"""
    evidence = [
        {
            "metric": "ctr",
            "display": "点击率",
            "dim_field": "spu_id",
            "dim_value": "SPU0123",
            "baseline": 0.031,
            "current": 0.021,
            "delta": -0.010,
            "delta_pct": -0.32,
            "z": -3.1,
        },
        {
            "metric": "clicks",
            "display": "点击量",
            "dim_field": "spu_id",
            "dim_value": "SPU0123",
            "baseline": 1200,
            "current": 820,
            "delta": -380,
            "delta_pct": -0.32,
            "z": -2.7,
        },
        {
            "metric": "impressions",
            "display": "曝光",
            "dim_field": "spu_id",
            "dim_value": "SPU0123",
            "baseline": 38000,
            "current": 36000,
            "delta": -2000,
            "delta_pct": -0.05,
            "z": -1.2,
        },
    ][:n_evidence]
    return {
        "card_id": card_id,
        "code": code,
        "title": title,
        "headline": "SPU0123 点击率明显下滑",
        "scope": {"dim": "spu_id", "dim_label": "款式", "dim_value": "SPU0123"},
        "severity": "high",
        "confidence": confidence,
        "primary_metric": "ctr",
        "window": {"start": "2026-11-11", "end": "2026-11-17"},
        "baseline_window": {"start": "2026-11-04", "end": "2026-11-10"},
        "evidence": evidence,
        "gmv_impact": -8800.0,
        "impact_share": 0.07,
        "narrative": "主图更换后点击率从 3.1% 降到 2.1%，承担约 7% 的成交额降幅。",
        "caveats": [],
        "candidates": [],
    }


def _verdict(
    card: dict,
    *,
    verdict: str = "support",
    after: float = 0.62,
    objections: list[str] | None = None,
    checks: list[Check] | None = None,
) -> Verdict:
    before = float(card["confidence"])
    return Verdict(
        card_id=str(card["card_id"]),
        code=str(card["code"]),
        title=str(card["title"]),
        verdict=verdict,
        confidence_before=before,
        confidence_after=after,
        factor=(after / before) if before else 1.0,
        objections=list(objections or []),
        checks=list(checks if checks is not None else [Check("sample", True, 1.0, "证据够")]),
    )


# ---------------------------------------------------------------------------
# 开关与回退
# ---------------------------------------------------------------------------


def test_no_llm_returns_original():
    cards = [_card()]
    verdicts = [_verdict(cards[0])]

    out, meta = review_cards(cards, verdicts)

    assert out == verdicts
    assert out[0] is verdicts[0]
    assert meta["requested"] is False
    assert meta["used"] == 0
    assert meta["degraded"] is False
    assert meta["reason"] == "未启用 LLM 复查"
    assert meta["cards"] == []
    assert meta["elapsed_ms"] >= 0.0


def test_llm_error_degrades_silently():
    cards = [_card(f"card_{i:03d}", code=f"R{i}") for i in (2, 3, 8)]
    verdicts = [_verdict(c, after=0.60) for c in cards]
    fake = FakeLLM(exc=LLMError("网关超时"))

    out, meta = review_cards(cards, verdicts, llm=fake)

    assert out == verdicts
    assert all(v.source == "rules" for v in out)
    assert meta["degraded"] is True
    assert meta["used"] == 0
    assert "第 1 张" in meta["reason"]
    assert "LLMError" in meta["reason"]
    # 网关级失败只试一次，不逐张烧预算。
    assert len(fake.calls) == 1


def test_non_numeric_confidence_falls_back():
    card = _card()
    rule = _verdict(card, after=0.62)
    fake = FakeLLM({"objections": ["只有一条证据"], "confidence": "高", "verdict": "weaken"})

    out, meta = review_cards([card], [rule], llm=fake)

    assert out[0] == rule
    assert out[0].source == "rules"
    assert meta["degraded"] is True
    assert meta["used"] == 0
    assert "第 1 张" in meta["reason"]
    assert "confidence" in meta["reason"]


def test_bad_json_structure_falls_back():
    card = _card()
    rule = _verdict(card, after=0.62)
    fake = FakeLLM(["not", "a", "dict"])  # 顶层不是对象

    out, meta = review_cards([card], [rule], llm=fake)

    assert out[0] is rule
    assert meta["degraded"] is True
    assert "JSON" in meta["reason"]


# ---------------------------------------------------------------------------
# 只降不升
# ---------------------------------------------------------------------------


def test_lower_confidence_downgrades_card():
    card = _card(confidence=0.70)
    rule = _verdict(card, verdict="support", after=0.62)
    body = {"objections": ["曝光也在跌，未必是主图问题"], "confidence": 0.40, "verdict": "weaken"}

    out, meta = review_cards([card], [rule], llm=FakeLLM(body))

    reviewed = out[0]
    # 模型给 0.40，但规则版 0.62 的 80% 地板是 0.496——单张卡最多压 20%，不照单全收。
    assert abs(reviewed.confidence_after - 0.496) < 1e-9
    assert reviewed.verdict == "weaken"
    assert reviewed.source == "llm"
    # confidence_before 保留规则版原值，factor 按新置信度反算。
    assert reviewed.confidence_before == 0.70
    assert abs(reviewed.factor - 0.496 / 0.70) < 1e-9
    assert meta["used"] == 1
    assert meta["degraded"] is False
    assert meta["cards"] == ["card_001"]


def test_model_cannot_raise_confidence():
    card = _card(confidence=0.60)
    rule = _verdict(card, verdict="weaken", after=0.40)
    body = {"objections": [], "confidence": 0.99, "verdict": "support"}

    out, _ = review_cards([card], [rule], llm=FakeLLM(body))

    assert out[0].confidence_after == 0.40
    assert out[0].confidence_after <= rule.confidence_after
    assert out[0].verdict == "weaken"


def test_illegal_verdict_recomputed_from_confidence():
    card = _card(confidence=0.70)
    rule = _verdict(card, verdict="support", after=0.62)
    body = {"objections": ["口径不一致"], "confidence": 0.45, "verdict": "maybe"}

    out, meta = review_cards([card], [rule], llm=FakeLLM(body))

    # 0.45 低于地板 0.496，所以取地板；verdict 由新置信度重算为 weaken。
    assert abs(out[0].confidence_after - 0.496) < 1e-9
    assert out[0].verdict == "weaken"
    assert meta["degraded"] is False


def test_checks_preserved_from_rule_version():
    card = _card(confidence=0.90, n_evidence=1)  # 只有一条证据，规则版会打折
    rule = critique([card])[0]
    body = {"objections": ["证据太薄"], "confidence": 0.35, "verdict": "weaken"}

    out, _ = review_cards([card], [rule], llm=FakeLLM(body))

    assert out[0].checks == rule.checks
    assert out[0].source == "llm"
    assert out[0].confidence_after <= rule.confidence_after


# ---------------------------------------------------------------------------
# 只复查前 N 张、跳过 reject
# ---------------------------------------------------------------------------


def test_only_first_max_cards_reviewed():
    cards = [_card(f"card_{i}", code=f"R{i}") for i in range(1, 6)]
    verdicts = [_verdict(c, after=0.60) for c in cards]
    fake = FakeLLM({"objections": [], "confidence": 0.50, "verdict": "weaken"})

    out, meta = review_cards(cards, verdicts, llm=fake, max_cards=2)

    assert len(fake.calls) == 2
    assert meta["used"] == 2
    assert meta["cards"] == ["card_1", "card_2"]
    assert out[2] is verdicts[2]
    assert out[2].source == "rules"


def test_rejected_cards_are_not_reviewed():
    cards = [_card("card_a", code="R1"), _card("card_b", code="R2"), _card("card_c", code="R3")]
    verdicts = [
        _verdict(cards[0], after=0.70),
        _verdict(cards[1], verdict="reject", after=0.20),
        _verdict(cards[2], verdict="weaken", after=0.45),
    ]
    fake = FakeLLM({"objections": [], "confidence": 0.50, "verdict": "weaken"})

    out, meta = review_cards(cards, verdicts, llm=fake, max_cards=3)

    assert len(fake.calls) == 2
    assert out[1] is verdicts[1]
    assert out[1].verdict == "reject"
    assert meta["used"] == 2
    assert meta["cards"] == ["card_a", "card_c"]


def test_budget_exhausted_stops_review():
    card = _card()
    rule = _verdict(card, after=0.62)
    fake = FakeLLM({"objections": [], "confidence": 0.50, "verdict": "weaken"})

    out, meta = review_cards([card], [rule], llm=fake, budget_ms=0)

    assert fake.calls == []
    assert out[0] is rule
    assert meta["degraded"] is True
    assert "预算" in meta["reason"]


# ---------------------------------------------------------------------------
# 反对意见合并
# ---------------------------------------------------------------------------


def test_objections_merged_deduped_and_capped():
    card = _card()
    rule_objections = ["方向不符", "证据太薄", "影响金额太小"]
    rule = _verdict(card, after=0.50, objections=rule_objections)
    body = {
        "objections": ["证据太薄", "曝光同步下滑", "促销口径不可比", "第四条"],
        "confidence": 0.40,
        "verdict": "weaken",
    }

    out, _ = review_cards([card], [rule], llm=FakeLLM(body))

    merged = out[0].objections
    assert merged[:3] == rule_objections
    assert merged.count("证据太薄") == 1
    assert len(merged) == 4
    assert merged[3] == "曝光同步下滑"


def test_model_objections_truncated_and_limited():
    card = _card()
    rule = _verdict(card, after=0.60, objections=[])
    body = {
        "objections": ["长" * 80, 123, "", "第二条", "第三条", "第四条"],
        "confidence": 0.50,
        "verdict": "weaken",
    }

    out, _ = review_cards([card], [rule], llm=FakeLLM(body))

    assert len(out[0].objections) == 3
    assert len(out[0].objections[0]) == 60
    assert out[0].objections[1:] == ["第二条", "第三条"]


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def test_prompt_is_compact_and_names_card():
    card = _card()
    fake = FakeLLM({"objections": [], "confidence": 0.55, "verdict": "weaken"})

    review_cards([card], [_verdict(card, after=0.60)], llm=fake)

    messages, kwargs = fake.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    system, user = messages[0]["content"], messages[1]["content"]
    assert "反方" in system
    assert card["code"] in user
    assert card["card_id"] not in user  # 不做无意义的标识符
    assert "evidence" in user
    # 只喂压缩后的事实，不是整份诊断 JSON。
    assert len(system) + len(user) < 2000
    assert kwargs["temperature"] == 0.0
    assert kwargs["max_tokens"] == 400
