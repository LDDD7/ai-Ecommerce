"""报告叙述器测试：全部离线，不碰真库，也不碰 app.core.llm。"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.orchestrator import narrator
from app.orchestrator.narrator import Narrative, narrate
OVERVIEW = {
    "as_of": "2026-11-17",
    "kpis": [
        {
            "name": "gmv",
            "label": "成交额",
            "unit": "currency",
            "value": 1014046.74,
            "prev": 2166647.29,
            "delta_pct": -53.2,
            "good": False,
        }
    ],
    "health": {
        "score": 49,
        "grade": "D",
        "parts": {"traffic": 11, "conversion": 64, "reputation": 69},
    },
}

DIAGNOSE = {
    "cards": [
        {
            "card_id": "card_003",
            "code": "R3",
            "title": "热销断货",
            "scope": {"dim": "sku_id", "dim_label": "商品", "dim_value": "SKU0292"},
            "severity": "high",
            "confidence": 0.83,
            "gmv_impact": -61015.79,
            "impact_share": 0.05,
            "narrative": "热销断货导致成交额 -61,016 元，占整体降幅 5%。",
            "evidence": ["SKU0292 可售天数 2 天"],
            "caveats": ["库存数据滞后 1 天"],
        }
    ],
    "gmv": {"summary": "成交额 2,166,647 → 1,014,047（-1,152,601，-53.2%）。"},
    "funnel": {"summary": "全店：曝光→支付 0.073% → 0.068%（-7.4%）。"},
    "meta": {"points": 65, "promo_share": {"skew": -0.57}},
}

PLAYBOOK = {
    "items": [
        {
            "card_id": "card_003",
            "code": "R3",
            "title": "热销断货",
            "gmv_impact": -61015.79,
            "actions": [
                {
                    "card_id": "PB-R3-01",
                    "title": "可售不足 3 天，紧急补货",
                    "action_type": "restock_alert",
                    "action_label": "补货预警",
                    "risk": 0,
                    "expected_effect": {
                        "metric": "stockout_rate",
                        "direction": "down",
                        "range": [0.3, 0.55],
                        "window_days": 7,
                    },
                }
            ],
        }
    ],
    "meta": {"cards": 1, "actions": 1},
}

TEMPLATE = [
    "截至 2026-11-17，成交额 1,014,047 元（-53.2%），健康分 49（D 级）。",
    "定位到 1 个问题：R3 热销断货。建议先执行「可售不足 3 天，紧急补货」。",
]

# 所有数字都只来自「压缩后」的事实：日期、千分位、百分比、摘要、动作标题。
LEGAL_TEXT = (
    "截至 2026-11-17，店铺成交额为 1,014,047 元，较上期 -53.2%，支付订单同步走弱。"
    "健康分 49，处于 D 级，整体表现偏弱，流量与转化都需要重新审视，"
    "其中流量是当前最明显的短板。\n"
    "按商品拆，SKU0292 的热销断货是主要问题，影响成交额 -61,016 元，占整体降幅 5%，"
    "需要优先处理。断货让可售天数压到 2 天，流量进来也接不住，建议先补货再谈投放。\n"
    "从漏斗看，曝光到支付的转化从 0.073% 降到 0.068%，降幅 -7.4%，说明流量下滑之外，"
    "承接效率也在变差。促销日的量价对比需要单独拆开看，不能只看大盘。\n"
    "动作上，先执行「可售不足 3 天，紧急补货」这张策略卡，把库存拉到安全线，"
    "再观察断货率是否回落；同时盯住转化与口碑，避免补货后仍然空转。"
)


class FakeLLM:
    """最小的假 LLM：只实现 duck typing 需要的 chat。"""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[tuple[list[dict], dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(text=self.text)


class RaisingLLM:
    def chat(self, messages, **kwargs):
        raise RuntimeError("上游超时")


class NoTextLLM:
    def chat(self, messages, **kwargs):
        return SimpleNamespace()


def _narrate(text: str | None, **kwargs):
    llm = None if text is None else FakeLLM(text)
    return narrate(
        None,
        OVERVIEW,
        DIAGNOSE,
        PLAYBOOK,
        template_paragraphs=TEMPLATE,
        llm=llm,
        **kwargs,
    )


def test_llm_disabled_returns_template():
    res = _narrate(None)
    assert res.source == "template"
    assert res.reason == "llm 未启用"
    assert res.paragraphs == TEMPLATE
    assert res.checks == {
        "numbers_ok": True,
        "bad_numbers": [],
        "chars": sum(len(p) for p in TEMPLATE),
    }


def test_legal_output_uses_llm():
    res = _narrate(LEGAL_TEXT)
    assert res.source == "llm"
    assert res.reason is None
    assert 3 <= len(res.paragraphs) <= 5
    assert res.checks["numbers_ok"] is True
    assert res.checks["bad_numbers"] == []
    assert res.checks["chars"] == sum(len(p) for p in res.paragraphs)


def test_invented_number_falls_back_whole_report():
    res = _narrate(LEGAL_TEXT + "\n另外，某渠道增长 1234%，需要复查。")
    assert res.source == "template"
    assert res.paragraphs == TEMPLATE
    assert res.checks["numbers_ok"] is False
    assert res.checks["bad_numbers"]
    assert any(b.startswith("1234") for b in res.checks["bad_numbers"])
    assert res.reason is not None and res.reason.startswith("数字校验失败")


def test_too_short_falls_back():
    res = _narrate("没有问题。")
    assert res.source == "template"
    assert res.reason is not None and "过短" in res.reason
    assert res.checks["chars"] < 200


def test_too_long_falls_back():
    res = _narrate("经营分析" * 300)
    assert res.source == "template"
    assert res.reason is not None and "过长" in res.reason
    assert res.checks["chars"] > 900


def test_markdown_table_and_code_block_are_stripped():
    noisy = (
        LEGAL_TEXT
        + "\n# 经营分析\n| 指标 | 数值 |\n| --- | --- |\n| 成交额 | 1,014,047 |"
        + "\n指标 | 数值\n成交额 | 1,014,047"
        + "\n```text\nprint(1234)\n```"
    )
    res = _narrate(noisy)
    assert res.source == "llm"
    assert len(res.paragraphs) == 4
    joined = "\n".join(res.paragraphs)
    assert "|" not in joined
    assert "```" not in joined
    assert "1234" not in joined


def test_llm_exception_falls_back_with_class_name():
    res = narrate(
        None,
        OVERVIEW,
        DIAGNOSE,
        PLAYBOOK,
        template_paragraphs=TEMPLATE,
        llm=RaisingLLM(),
    )
    assert res.source == "template"
    assert res.paragraphs == TEMPLATE
    assert res.reason is not None and "RuntimeError" in res.reason


def test_llm_result_without_text_attribute_falls_back():
    res = narrate(
        None,
        OVERVIEW,
        DIAGNOSE,
        PLAYBOOK,
        template_paragraphs=TEMPLATE,
        llm=NoTextLLM(),
    )
    assert res.source == "template"
    assert res.reason is not None and "AttributeError" in res.reason


def test_thousands_and_percent_forms_pass_check():
    paragraphs = LEGAL_TEXT.split("\n")
    text = "\n".join([paragraphs[0], paragraphs[0], paragraphs[1]])
    assert "1,014,047" in text and "-53.2%" in text
    res = _narrate(text)
    assert res.source == "llm"
    assert res.checks["numbers_ok"] is True
    assert res.checks["bad_numbers"] == []


def test_absolute_value_of_negative_fact_passes():
    # 事实里是 -1,152,601，但自然中文常写「减少 1,152,601 元」；绝对值写法应放行。
    res = _narrate(LEGAL_TEXT + "\n成交额减少 1,152,601 元，降幅 53.2%。")
    assert res.source == "llm"
    assert res.checks["numbers_ok"] is True
    assert res.checks["bad_numbers"] == []


def test_as_dict_has_all_fields_and_copies_lists():
    node = Narrative(paragraphs=["甲", "乙"], source="template", reason=None, checks={"numbers_ok": True})
    data = node.as_dict()
    assert set(data) == {"paragraphs", "source", "reason", "checks"}
    assert data["paragraphs"] == ["甲", "乙"]
    assert data["source"] == "template"
    assert data["checks"]["numbers_ok"] is True

    data["paragraphs"].append("丙")
    assert node.paragraphs == ["甲", "乙"]


def test_prompt_carries_rules_facts_actions_and_template():
    fake = FakeLLM(LEGAL_TEXT)
    narrate(
        SimpleNamespace(kind="diagnose", window_days=7),
        OVERVIEW,
        DIAGNOSE,
        PLAYBOOK,
        template_paragraphs=TEMPLATE,
        llm=fake,
        max_tokens=321,
    )
    messages, kwargs = fake.calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]

    assert "电商店铺经营分析师" in system
    for rule in (
        "不得计算新数字",
        "markdown",
        "200~420 字",
        "3~4 段",
        "每段不超过 3 句",
        "不得编造",
        "总结",
    ):
        assert rule in system

    assert "【事实】" in user and "【建议动作】" in user and "【参考写法】" in user
    # 只发送压缩后的事实：完整工具结果里的字段不应再出现在提示词里。
    for hidden in ('"prev"', '"impact_share"', '"confidence"', '"meta"', '"parts"', '"caveats"'):
        assert hidden not in user
    assert '"name": "gmv"' in user
    assert '"value": 1014046.74' in user
    assert '"delta_pct": -53.2' in user
    assert '"score": 49' in user and '"grade": "D"' in user
    assert '"dim_value": "SKU0292"' in user
    assert '"gmv_impact": -61015.79' in user
    assert '"range": [' in user
    assert "【本次问题】" in user and "window_days=7" in user
    assert "2026-11-17" in user
    assert "可售不足 3 天，紧急补货" in user
    assert TEMPLATE[0] in user
    assert kwargs["max_tokens"] == 321


def test_prompt_caps_cards_evidence_and_actions():
    cards = []
    for index in range(1, 6):
        cards.append(
            {
                "code": f"R{index}",
                "title": f"问题{index}",
                "scope": {"dim_value": f"SKU{index:04d}"},
                "severity": "high",
                "confidence": 0.5 + index / 100,
                "gmv_impact": -1000.0 * index,
                "impact_share": 0.01 * index,
                "narrative": f"问题{index}影响 -{1000 * index} 元。",
                "evidence": [
                    {
                        "metric": "stockout_rate",
                        "dim_value": f"SKU{index:04d}",
                        "baseline": 10.0,
                        "current": 5.0,
                        "delta_pct": -50.0,
                        "secret_extra": 999,
                    }
                ],
                "caveats": ["隐藏备注 777"],
            }
        )
    actions = [
        {
            "title": f"动作{index}",
            "risk": index,
            "expected_effect": {
                "metric": "m",
                "range": [0.1, 0.2],
                "window_days": 7,
            },
        }
        for index in range(1, 9)
    ]
    diagnose = {
        "cards": cards,
        "gmv": {"summary": "gmv 摘要"},
        "funnel": {"summary": "funnel 摘要"},
        "meta": {"points": 65},
    }
    playbook = {
        "items": [{"code": "R1", "title": "问题1", "actions": actions}],
        "meta": {"cards": 1, "actions": 8},
    }
    fake = FakeLLM(LEGAL_TEXT)
    narrate(
        None,
        OVERVIEW,
        diagnose,
        playbook,
        template_paragraphs=TEMPLATE,
        llm=fake,
    )
    user = fake.calls[0][0][1]["content"]

    assert '"code": "R3"' in user
    assert '"code": "R4"' not in user and '"code": "R5"' not in user
    assert "动作6" in user
    assert "动作7" not in user
    assert "secret_extra" not in user
    assert "隐藏备注" not in user
    assert '"meta"' not in user
    assert "65" not in user


def test_default_budget_matches_compressed_prompt():
    assert inspect.signature(narrate).parameters["max_tokens"].default == 500
    assert narrator._MAX_CHARS == 900
    assert "200~420 字" in narrator.SYSTEM_PROMPT


def test_fact_string_digits_are_allowed():
    # 日期、SKU 编号、摘要里的数字都属于事实，复述它们不应判失败。
    text = "\n".join([LEGAL_TEXT.split("\n")[1], LEGAL_TEXT.split("\n")[2], LEGAL_TEXT.split("\n")[3]])
    res = _narrate(text)
    assert res.source == "llm"
    assert res.checks["numbers_ok"] is True
