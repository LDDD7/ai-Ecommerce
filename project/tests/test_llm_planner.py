"""LLM 计划器测试：模型只出草稿，校验与降级在本地。"""

from __future__ import annotations

from types import SimpleNamespace

from app.orchestrator.intents import parse_intent
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


class FakeLLM:
    """只实现 plan 需要的 chat_json，返回预设 body 或抛预设异常。"""

    def __init__(self, body=None, error: Exception | None = None, text: str = "") -> None:
        self.body = body
        self.error = error
        self.text = text
        self.calls: list[list[dict]] = []

    def chat_json(self, messages, **_kw):  # noqa: ANN001
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.body

    def chat(self, messages, **_kw):  # noqa: ANN001
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


def _plan(body, question="为什么成交额下滑了", error=None):
    llm = FakeLLM(body=body, error=error)
    planner = LLMPlanner(llm=llm)
    return planner.plan(parse_intent(question)), planner


def test_valid_plan_passes_through():
    steps, planner = _plan(
        {
            "steps": [
                {"tool": TOOL_DIAGNOSE, "args": {"window_days": 7}, "why": "扫异常"},
                {"tool": TOOL_OVERVIEW, "args": {"window_days": 7}, "why": "看大盘"},
            ]
        }
    )
    # 大盘永远排最前
    assert [s.tool for s in steps] == [TOOL_OVERVIEW, TOOL_DIAGNOSE]
    assert planner.last_trace.ok is True
    assert planner.last_trace.steps == [TOOL_OVERVIEW, TOOL_DIAGNOSE]


def test_unknown_tool_is_dropped_not_executed():
    steps, planner = _plan(
        {"steps": [{"tool": "shell.exec", "args": {}, "why": "rm -rf"}, {"tool": TOOL_OVERVIEW, "why": "看大盘"}]}
    )
    assert [s.tool for s in steps] == [TOOL_OVERVIEW]
    assert "shell.exec" in planner.last_trace.dropped


def test_args_are_clamped_and_unknown_keys_ignored():
    steps, _ = _plan(
        {
            "steps": [
                {
                    "tool": TOOL_OVERVIEW,
                    "args": {"window_days": 999, "as_of": "上周三", "sql": "DROP TABLE fact_orders"},
                    "why": "看大盘",
                }
            ]
        }
    )
    args = steps[0].args
    assert args["window_days"] == 30  # 夹到上限
    assert args["as_of"] is None  # 非法日期被丢掉，回落到意图
    assert "sql" not in args


def test_missing_dependency_is_inserted():
    steps, _ = _plan({"steps": [{"tool": TOOL_PLAYBOOK, "why": "配动作"}]})
    assert [s.tool for s in steps] == [TOOL_DIAGNOSE, TOOL_PLAYBOOK]
    assert steps[0].why.startswith("补齐前置")


def test_duplicate_tools_kept_once():
    steps, _ = _plan(
        {
            "steps": [
                {"tool": TOOL_OVERVIEW, "why": "一"},
                {"tool": TOOL_OVERVIEW, "why": "二"},
                {"tool": TOOL_DIAGNOSE, "why": "三"},
            ]
        }
    )
    assert [s.tool for s in steps] == [TOOL_OVERVIEW, TOOL_DIAGNOSE]


def test_step_count_is_capped():
    steps, _ = _plan(
        {
            "steps": [
                {"tool": TOOL_OVERVIEW, "why": "a"},
                {"tool": TOOL_DIAGNOSE, "why": "b"},
                {"tool": TOOL_DECOMPOSE, "why": "c"},
                {"tool": TOOL_PLAYBOOK, "why": "d"},
                {"tool": TOOL_ACTION, "why": "e"},
                {"tool": TOOL_OVERVIEW, "why": "重复的会被去重"},
            ]
        }
    )
    # 去重后 5 个，正好等于上限；TOOL_ACTION 在前，overview 被提到最前
    assert len(steps) == 5
    assert [s.tool for s in steps] == [
        TOOL_OVERVIEW,
        TOOL_DIAGNOSE,
        TOOL_DECOMPOSE,
        TOOL_PLAYBOOK,
        TOOL_ACTION,
    ]


def test_empty_plan_falls_back_to_rules():
    steps, planner = _plan({"steps": []})
    rule = RulePlanner().plan(parse_intent("为什么成交额下滑了"))
    assert [s.tool for s in steps] == [s.tool for s in rule]
    assert planner.last_trace.ok is False
    assert "为空" in (planner.last_trace.reason or "")


def test_llm_error_falls_back_to_rules():
    steps, planner = _plan(None, error=TimeoutError("model timeout"))
    rule = RulePlanner().plan(parse_intent("为什么成交额下滑了"))
    assert [s.tool for s in steps] == [s.tool for s in rule]
    assert "TimeoutError" in (planner.last_trace.reason or "")


def test_non_dict_plan_raises_and_falls_back():
    steps, planner = _plan(["not", "a", "dict"])
    assert steps, "回退后应当有步骤"
    assert planner.last_trace.ok is False


def test_prompt_contains_question_and_tools():
    llm = FakeLLM(body={"steps": [{"tool": TOOL_OVERVIEW, "why": "看大盘"}]})
    LLMPlanner(llm=llm).plan(parse_intent("甜美款这周为什么掉得厉害"))
    joined = "\n".join(m["content"] for m in llm.calls[0])
    assert "甜美款这周为什么掉得厉害" in joined
    for tool in (TOOL_OVERVIEW, TOOL_DIAGNOSE, TOOL_DECOMPOSE, TOOL_PLAYBOOK):
        assert tool in joined
    assert "只输出 JSON" in joined


def test_make_planner_returns_rule_without_key(monkeypatch):
    from app.core import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("LLM_MODE", "mock")
    try:
        assert isinstance(make_planner(), RulePlanner)
    finally:
        config.get_settings.cache_clear()


def test_make_planner_uses_injected_llm():
    planner = make_planner(llm=FakeLLM(body={"steps": [{"tool": TOOL_OVERVIEW, "why": "x"}]}))
    assert isinstance(planner, LLMPlanner)


def test_trace_as_dict_is_serialisable():
    _, planner = _plan({"steps": [{"tool": TOOL_OVERVIEW, "why": "看大盘"}]})
    trace = planner.last_trace.as_dict()
    assert trace["planner"] == "llm-v1"
    assert trace["ok"] is True
    assert trace["steps"] == [TOOL_OVERVIEW]
    assert trace["elapsed_ms"] is not None
