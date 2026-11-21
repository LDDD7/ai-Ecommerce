"""编排层测试：意图解析、计划、事件流、报告文案。"""

from __future__ import annotations

import asyncio
import json

from app.orchestrator.events import ChatEvent, to_sse
from app.orchestrator.intents import parse_intent
from app.orchestrator.planner import (
    TOOL_ACTION,
    TOOL_DECOMPOSE,
    TOOL_DIAGNOSE,
    TOOL_OVERVIEW,
    TOOL_PLAYBOOK,
    RulePlanner,
)
from app.orchestrator.report import compose
from app.orchestrator.runner import run_chat
from app.orchestrator.tools import call_tool


def test_intent_diagnose_with_style_and_window():
    it = parse_intent("近14天甜美连衣裙为什么转化掉了？")
    assert it.kind == "diagnose"
    assert it.window_days == 14
    assert it.styles == ["甜美"]
    assert it.metrics == ["cvr"]


def test_intent_overview_default():
    it = parse_intent("店铺整体怎么样")
    assert it.kind == "overview"
    assert it.window_days == 7


def test_intent_metric_and_sku():
    it = parse_intent("SKU0292 的退款率是多少")
    assert it.kind in ("metric", "diagnose")
    assert it.sku == "SKU0292"
    assert "refund_rate" in it.metrics


def test_intent_explicit_date():
    it = parse_intent("2026-11-17 那天大盘怎么样")
    assert it.as_of == "2026-11-17"


def test_planner_diagnose_has_five_tools():
    steps = RulePlanner().plan(parse_intent("为什么成交额下滑了"))
    assert [s.tool for s in steps] == [
        TOOL_OVERVIEW,
        TOOL_DIAGNOSE,
        TOOL_DECOMPOSE,
        TOOL_PLAYBOOK,
        TOOL_ACTION,
    ]


def test_planner_overview_has_one_tool():
    steps = RulePlanner().plan(parse_intent("大盘怎么样"))
    assert [s.tool for s in steps] == [TOOL_OVERVIEW]


def test_sse_encoding():
    frame = to_sse(ChatEvent("token", {"text": "您好"}))
    assert frame.startswith("event: token\n")
    assert frame.endswith("\n\n")
    payload = frame.split("data: ", 1)[1].strip()
    assert json.loads(payload) == {"text": "您好"}


def test_event_rejects_unknown_name():
    try:
        ChatEvent("nope", {})
    except ValueError as exc:
        assert "未知事件类型" in str(exc)
    else:  # pragma: no cover - 正常不会走到
        raise AssertionError("应当拒绝未知事件类型")


def test_run_chat_event_sequence():
    async def collect():
        return [ev async for ev in run_chat("为什么 11-17 成交额下滑", as_of="2026-11-17")]

    events = asyncio.run(collect())
    names = [e.name for e in events]
    assert names[0] == "plan"
    assert names[-1] == "done"
    assert "tool_call" in names and "tool_result" in names
    assert names.count("tool_call") == 5
    assert "evidence" in names
    assert "token" in names
    assert "playbook" in names
    assert "action_draft" in names
    playbook = next(e for e in events if e.name == "playbook")
    assert playbook.data["status"] == "ok"
    assert playbook.data["items"], "诊断卡应当配上动作"
    assert playbook.data["meta"]["actions"] >= 1
    for item in playbook.data["items"]:
        assert {"card_id", "code", "title", "actions"} <= set(item)
        for action in item["actions"]:
            assert action["risk"] in (0, 1, 2)
            assert action["action_label"]
    done = events[-1].data
    assert done["cards"] >= 1
    assert done["playbook_actions"] >= 1
    assert done["window_days"] == 7


def test_run_chat_tokens_form_readable_report():
    async def text_of():
        chunks = []
        async for ev in run_chat("近7天大盘怎么样", as_of="2026-11-17"):
            if ev.name == "token":
                chunks.append(ev.data["text"])
        return "".join(chunks)

    text = asyncio.run(text_of())
    assert "成交额" in text
    assert "健康分" in text
    assert "为什么这周成交额下滑" in text


PLAYBOOK_FIXTURE = {
    "items": [
        {
            "card_id": "card_001",
            "code": "R3",
            "title": "热销断货",
            "gmv_impact": -61016.0,
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
                        "range": [0.15, 0.35],
                        "window_days": 7,
                    },
                },
                {
                    "card_id": "PB-R9-01",
                    "title": "回调成交价至主流价格带",
                    "action_type": "adjust_price",
                    "action_label": "调价",
                    "risk": 2,
                    "expected_effect": None,
                },
            ],
        }
    ],
    "meta": {"cards": 1, "actions": 2},
}


def test_report_prefers_playbook_actions():
    diagnose = {
        "gmv": {"summary": "成交额 100 → 90（-10，-10.0%）。"},
        "funnel": {"summary": "全店：曝光→支付 0.07% → 0.06%。"},
        "cards": [
            {
                "card_id": "card_001",
                "code": "R3",
                "title": "热销断货",
                "scope": {"dim_label": "商品", "dim_value": "SKU0292"},
                "confidence": 0.8,
                "narrative": "断货导致成交额少了 6.1 万。",
                "caveats": [],
            }
        ],
        "meta": {"promo_share": {"skew": 0.0}},
    }
    paras = compose(parse_intent("为什么成交额下滑"), None, diagnose, PLAYBOOK_FIXTURE)
    text = "\n".join(paras)
    assert "建议动作" in text
    assert "可售不足 3 天，紧急补货" in text
    assert "补货预警" in text
    assert "risk 0" in text and "低风险" in text
    assert "risk 2" in text and "必须人审" in text
    assert "预期 7 天内 stockout_rate 下降 15%~35%" in text
    # 有策略动作时不再回落到固定的 NEXT_STEP 文案
    assert "下一步可以这样动" not in text


def test_report_falls_back_when_playbook_empty():
    diagnose = {
        "gmv": {"summary": "成交额 100 → 90。"},
        "funnel": {"summary": "漏斗摘要。"},
        "cards": [
            {
                "card_id": "card_001",
                "code": "R3",
                "title": "热销断货",
                "scope": {"dim_label": "商品", "dim_value": "SKU0292"},
                "confidence": 0.8,
                "narrative": "断货导致成交额少了 6.1 万。",
                "caveats": [],
            }
        ],
        "meta": {},
    }
    paras = compose(parse_intent("为什么成交额下滑"), None, diagnose, {"items": [], "meta": {}})
    text = "\n".join(paras)
    assert "下一步可以这样动" in text
    assert "优先补货断货 SKU" in text


def test_playbook_tool_requires_cards():
    res = call_tool("playbook.search", "data/store.db", cards=None)
    assert res.ok is False
    assert "diagnose.run" in (res.error or "")
