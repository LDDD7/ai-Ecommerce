"""编排执行器：把「问一句」跑成一串 SSE 事件。

状态推进：parse_intent → plan → tool_loop（概览 / 诊断 / 分解 / 配动作）→
diagnosis+evidence 事件 → playbook 事件 → report 流式 token → done。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import anyio

from app.core.config import get_settings
from app.orchestrator.events import ChatEvent
from app.orchestrator.intents import Intent, parse_intent
from app.orchestrator.planner import (
    TOOL_ACTION,
    TOOL_DECOMPOSE,
    TOOL_DIAGNOSE,
    TOOL_OVERVIEW,
    TOOL_PLAYBOOK,
    LLMPlanner,
    RulePlanner,
    make_planner,
)
from app.orchestrator.report import compose
from app.orchestrator.tools import ToolResult, call_tool

CHUNK_CHARS = 26
"""流式 token 的切片长度：够小才有打字感，够大才不会刷屏。"""


@dataclass(slots=True)
class _Ctx:
    db_path: str
    as_of: str | None
    window_days: int
    payloads: dict[str, dict[str, Any]]
    trace_id: str


def _db_path() -> str:
    settings = get_settings()
    return str(Path(settings.db_path))


async def _narrate(
    intent: Intent,
    overview: dict[str, Any] | None,
    diagnose: dict[str, Any] | None,
    playbook: dict[str, Any] | None,
    template_paragraphs: list[str],
    *,
    spent_ms: float = 0.0,
) -> tuple[list[str], str, str | None]:
    """用 LLM 重写报告；没开、超预算或失败都退回模板。

    文案是锦上添花，不能因为它失败就把整轮诊断丢掉——所以这里的 except 是兜底而不是错误。
    """
    settings = get_settings()
    from app.core import runtime

    if runtime.llm_active() != "real":
        return template_paragraphs, "template", "llm 未启用"
    if spent_ms > settings.report_budget_ms:
        # 工具链已经吃掉了大部分预算，再等模型改写就必然超 8s：直接交模板
        return (
            template_paragraphs,
            "template",
            f"已用 {spent_ms:.0f}ms，超出报告预算 {settings.report_budget_ms}ms，跳过模型改写",
        )
    try:
        from app.core.llm import get_client
        from app.orchestrator.narrator import narrate

        narration = await anyio.to_thread.run_sync(
            lambda: narrate(
                intent,
                overview,
                diagnose,
                playbook,
                template_paragraphs=template_paragraphs,
                llm=get_client(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return template_paragraphs, "template", f"{type(exc).__name__}: {exc}"
    return narration.paragraphs, narration.source, narration.reason


async def _run_tool(name: str, ctx: _Ctx, why: str) -> tuple[ToolResult, float]:
    t0 = time.perf_counter()
    if name == TOOL_DECOMPOSE and TOOL_DIAGNOSE in ctx.payloads:
        # 分解结果就在诊断结果里，复用即可（否则同一份数据要算两遍，白等 1s）
        gmv = ctx.payloads[TOOL_DIAGNOSE]["gmv"]
        res = ToolResult(TOOL_DECOMPOSE, True, gmv, gmv.get("summary", ""))
    elif name == TOOL_PLAYBOOK:
        # 配动作要拿诊断卡当输入；诊断没跑过就让工具自己报错（变成 error 事件）
        cards = (ctx.payloads.get(TOOL_DIAGNOSE) or {}).get("cards")
        res = await anyio.to_thread.run_sync(
            lambda: call_tool(
                name,
                ctx.db_path,
                as_of=ctx.as_of,
                window_days=ctx.window_days,
                cards=cards,
            )
        )
    elif name == TOOL_ACTION:
        # 起草要同时拿诊断卡和策略动作：缺一个都只生成一半，宁可让工具报错
        cards = (ctx.payloads.get(TOOL_DIAGNOSE) or {}).get("cards")
        playbook = ctx.payloads.get(TOOL_PLAYBOOK)
        res = await anyio.to_thread.run_sync(
            lambda: call_tool(
                name,
                ctx.db_path,
                as_of=ctx.as_of,
                window_days=ctx.window_days,
                cards=cards,
                playbook=playbook,
                trace_id=ctx.trace_id,
            )
        )
    else:
        res = await anyio.to_thread.run_sync(
            lambda: call_tool(name, ctx.db_path, as_of=ctx.as_of, window_days=ctx.window_days)
        )
    if res.ok and res.payload:
        ctx.payloads[name] = res.payload
    return res, round((time.perf_counter() - t0) * 1000, 1)


async def run_chat(
    question: str,
    as_of: str | None = None,
    window_days: int = 7,
    db_path: str | None = None,
) -> AsyncIterator[ChatEvent]:
    """跑一次对话，逐条 yield 事件。调用方负责编码成 SSE。"""
    t0 = time.perf_counter()
    intent: Intent = parse_intent(question)
    if as_of:
        intent.as_of = as_of
    if window_days != 7:
        intent.window_days = window_days

    planner = make_planner()
    steps = planner.plan(intent)
    plan_payload: dict[str, Any] = {
        "intent": intent.as_dict(),
        "planner": planner.name,
        "steps": [s.as_dict() for s in steps],
    }
    if isinstance(planner, LLMPlanner):
        # 模型出的计划不一定照单全收：把丢弃项、降级原因一起摊给前端/trace 看
        plan_payload["planner_trace"] = planner.last_trace.as_dict()
    yield ChatEvent("plan", plan_payload)

    ctx = _Ctx(
        db_path=db_path or _db_path(),
        as_of=intent.as_of,
        window_days=intent.window_days,
        payloads={},
        trace_id=f"tr_{uuid.uuid4().hex[:8]}",
    )
    diagnose_payload: dict[str, Any] | None = None

    for step in steps:
        yield ChatEvent("tool_call", {"tool": step.tool, "args": step.args, "why": step.why})
        result, ms = await _run_tool(step.tool, ctx, step.why)
        yield ChatEvent(
            "tool_result",
            {
                "tool": step.tool,
                "ok": result.ok,
                "summary": result.summary,
                "error": result.error,
                "elapsed_ms": ms,
            },
        )
        if not result.ok:
            yield ChatEvent("error", {"tool": step.tool, "message": result.error or "工具执行失败"})
            continue

        if step.tool == TOOL_OVERVIEW:
            body = result.payload
            yield ChatEvent(
                "diagnosis",
                {
                    "kind": "overview",
                    "as_of": body.get("as_of"),
                    "health": body.get("health"),
                    "kpis": body.get("kpis"),
                },
            )
        elif step.tool == TOOL_DIAGNOSE:
            diagnose_payload = result.payload
            for card in diagnose_payload.get("cards", []):
                yield ChatEvent(
                    "diagnosis",
                    {
                        "kind": "card",
                        "card_id": card["card_id"],
                        "code": card["code"],
                        "title": card["title"],
                        "scope": card["scope"],
                        "severity": card["severity"],
                        "confidence": card["confidence"],
                        "gmv_impact": card["gmv_impact"],
                        "impact_share": card["impact_share"],
                        "narrative": card["narrative"],
                    },
                )
                for ev in card.get("evidence", [])[:3]:
                    yield ChatEvent("evidence", {"card_id": card["card_id"], **ev})
            critic = (diagnose_payload.get("meta") or {}).get("critic") or {}
            if critic:
                replan = critic.get("replan") or {}
                yield ChatEvent(
                    "critic",
                    {
                        "engine": critic.get("engine"),
                        "checked": critic.get("checked", 0),
                        "support": critic.get("support", 0),
                        "weaken": critic.get("weaken", 0),
                        "reject": critic.get("reject", 0),
                        "elapsed_ms": critic.get("elapsed_ms"),
                        "replan": {
                            "enabled": replan.get("enabled", False),
                            "days": replan.get("days"),
                            "upgrade": replan.get("upgrade", 0),
                            "drop": replan.get("drop", 0),
                            "demote": replan.get("demote", 0),
                            "skip": replan.get("skip", 0),
                            "degraded": replan.get("degraded", False),
                            "elapsed_ms": replan.get("elapsed_ms"),
                            "items": replan.get("items", []),
                        },
                    },
                )
        elif step.tool == TOOL_PLAYBOOK:
            body = result.payload
            yield ChatEvent(
                "playbook",
                {
                    "status": "ok",
                    "items": body.get("items", []),
                    "meta": body.get("meta", {}),
                },
            )
        elif step.tool == TOOL_ACTION:
            body = result.payload
            yield ChatEvent(
                "action_draft",
                {
                    "status": "ok",
                    "store_id": "store_001",
                    "items": body.get("items", []),
                    "meta": body.get("meta", {}),
                },
            )

    template_paragraphs = compose(
        intent,
        ctx.payloads.get(TOOL_OVERVIEW),
        diagnose_payload,
        ctx.payloads.get(TOOL_PLAYBOOK),
        ctx.payloads.get(TOOL_ACTION),
    )
    paragraphs, report_source, report_reason = await _narrate(
        intent,
        ctx.payloads.get(TOOL_OVERVIEW),
        diagnose_payload,
        ctx.payloads.get(TOOL_PLAYBOOK),
        template_paragraphs,
        spent_ms=(time.perf_counter() - t0) * 1000,
    )
    for para in paragraphs:
        for i in range(0, len(para), CHUNK_CHARS):
            yield ChatEvent("token", {"text": para[i : i + CHUNK_CHARS]})
            await asyncio.sleep(0)
        yield ChatEvent("token", {"text": "\n"})

    playbook_actions = (ctx.payloads.get(TOOL_PLAYBOOK) or {}).get("meta", {}).get("actions", 0)
    yield ChatEvent(
        "done",
        {
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "as_of": intent.as_of or get_settings().demo_as_of,
            "window_days": intent.window_days,
            "tools": [s.tool for s in steps],
            "cards": len(diagnose_payload.get("cards", [])) if diagnose_payload else 0,
            "playbook_actions": playbook_actions,
            "actions": len((ctx.payloads.get(TOOL_ACTION) or {}).get("items", [])),
            "trace_id": ctx.trace_id,
            "report_source": report_source,
            "report_reason": report_reason,
        },
    )
