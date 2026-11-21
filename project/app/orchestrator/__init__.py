"""对话编排：意图 → 计划 → 工具调用 → 诊断 → 报告。

本包只做「串流程」，每一步的计算都在 diagnose / insights / metrics 里。
没有 LLM key 时由 `RulePlanner` 顶替，接口与将来的 LLM planner 一致。
"""

from app.orchestrator.events import ChatEvent, to_sse
from app.orchestrator.intents import Intent, parse_intent
from app.orchestrator.planner import RulePlanner, Step
from app.orchestrator.runner import run_chat

__all__ = [
    "ChatEvent",
    "Intent",
    "RulePlanner",
    "Step",
    "parse_intent",
    "run_chat",
    "to_sse",
]
