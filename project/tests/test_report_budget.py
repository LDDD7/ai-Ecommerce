"""报告改写的两个保护：没卡/没动作时别诱导编造，以及超预算直接交模板。"""

from __future__ import annotations

import asyncio

import pytest

from app.orchestrator import narrator as narrator_mod
from app.orchestrator.runner import _narrate

TEMPLATE = ["第一段模板。", "第二段模板。"]


def _facts(cards: list[dict], actions: list[dict]) -> tuple[dict, dict]:
    overview = {"as_of": "2026-11-17", "window": {"days": 7}, "kpis": [], "health": {"score": 49, "grade": "D"}}
    diagnose = {"gmv": {"summary": "成交额下降"}, "funnel": {"summary": "漏斗变差"}, "cards": cards}
    playbook = {"items": [{"title": "补货", "actions": actions}] if actions else []}
    return overview, diagnose


def test_prompt_drops_card_rule_when_no_cards():
    overview, diagnose = _facts([], [])
    facts = narrator_mod._compact_facts(overview, diagnose, {"items": []}, {})
    user = narrator_mod._build_messages(facts, TEMPLATE)[1]["content"]
    assert "本次没有诊断卡" in user
    assert "只写影响最大的 2 张卡" not in user


def test_prompt_drops_action_rule_when_no_actions():
    overview, diagnose = _facts(
        [{"code": "R3", "title": "断货", "severity": "high", "gmv_impact": -100, "narrative": "n"}],
        [],
    )
    facts = narrator_mod._compact_facts(overview, diagnose, {"items": []}, {})
    user = narrator_mod._build_messages(facts, TEMPLATE)[1]["content"]
    assert "不要提具体动作" in user


def test_prompt_keeps_rules_when_cards_and_actions_exist():
    overview, diagnose = _facts(
        [{"code": "R3", "title": "断货", "severity": "high", "gmv_impact": -100, "narrative": "n"}],
        [{"title": "紧急补货", "risk": 0}],
    )
    playbook = {
        "items": [
            {
                "title": "断货",
                "actions": [
                    {
                        "card_id": "PB-R3-01",
                        "title": "紧急补货",
                        "risk": 0,
                        "expected_effect": {"metric": "stockout_rate", "range": [0.15, 0.35]},
                    }
                ],
            }
        ]
    }
    facts = narrator_mod._compact_facts(overview, diagnose, playbook, {})
    user = narrator_mod._build_messages(facts, TEMPLATE)[1]["content"]
    assert "只写影响最大的 2 张卡" in user
    assert "本次没有可用策略卡" not in user


def test_narrate_skips_llm_when_over_budget(monkeypatch):
    from app.core import config

    monkeypatch.setenv("LLM_MODE", "real")
    config.get_settings.cache_clear()
    called = {"n": 0}

    class _Boom:
        def chat_json(self, *args, **kwargs):  # noqa: ANN002, ANN003
            called["n"] += 1
            raise AssertionError("超预算时不应调用模型")

    paragraphs, source, reason = asyncio.run(
        _narrate(None, None, None, None, TEMPLATE, spent_ms=99_000)
    )
    assert paragraphs == TEMPLATE
    assert source == "template"
    assert "超出报告预算" in (reason or "")
    assert called["n"] == 0
    config.get_settings.cache_clear()
