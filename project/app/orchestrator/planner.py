"""计划器：意图 → 工具调用序列。

`RulePlanner` 是零依赖、可离线跑的版本；将来接 LLM 时只要实现同样的
`plan(intent) -> list[Step]`，编排层不用改。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import get_settings
from app.orchestrator.intents import Intent


@dataclass(slots=True)
class Step:
    """一次工具调用。`args` 会原样传给工具函数。"""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "why": self.why}


class Planner(Protocol):
    name: str

    def plan(self, intent: Intent) -> list[Step]: ...


TOOL_OVERVIEW = "insights.overview"
TOOL_DIAGNOSE = "diagnose.run"
TOOL_DECOMPOSE = "diagnose.decompose"
TOOL_PLAYBOOK = "playbook.search"
TOOL_ACTION = "action.draft"


class RulePlanner:
    """按意图配工具：大盘类先看概览；诊断类概览 + 诊断 + 分解 + 配动作。"""

    name = "rule-v1"

    def plan(self, intent: Intent) -> list[Step]:
        args = {"as_of": intent.as_of, "window_days": intent.window_days}
        steps = [Step(TOOL_OVERVIEW, dict(args), "先看大盘与健康分，确认有没有异常")]
        if intent.kind == "diagnose":
            steps.append(Step(TOOL_DIAGNOSE, dict(args), "按维度扫描异常并给根因假设"))
            steps.append(Step(TOOL_DECOMPOSE, dict(args), "拆成交额恒等式，量化各因子贡献"))
            steps.append(Step(TOOL_PLAYBOOK, dict(args), "按根因给每张诊断卡配可执行动作"))
            steps.append(Step(TOOL_ACTION, dict(args), "选好动作后起草草案，等人审批"))
        elif intent.kind == "metric":
            steps.append(Step(TOOL_DIAGNOSE, dict(args), "查数顺带确认是否伴随异常"))
        return steps


# ---- LLM 计划器 ----

# 工具白名单：模型只能从这里挑，写别的名字一律丢弃（不报错、不执行）。
TOOL_CATALOG: dict[str, str] = {
    TOOL_OVERVIEW: "看大盘：成交额/订单/转化/退款/ROI + 健康分。任何问题都建议先调一次。",
    TOOL_DIAGNOSE: "按款式/渠道/商品扫描异常并给根因假设（R1..R10）+ 证据。问「为什么」时必调。",
    TOOL_DECOMPOSE: "拆成交额恒等式 GMV=流量×转化率×客单价，量化各因子贡献。要解释跌幅构成时调。",
    TOOL_PLAYBOOK: "按诊断出的根因配可执行策略动作（含风险等级与预期效果）。诊断之后才调。",
    TOOL_ACTION: "把选中的策略动作落成待审批的动作草案（调价/出价/换素材/发券/补货）。配完动作才调。",
}

# 依赖关系：这些工具要有前置结果才能跑，模型漏了就自动补上。
DEPENDS_ON: dict[str, str] = {
    TOOL_DECOMPOSE: TOOL_DIAGNOSE,
    TOOL_PLAYBOOK: TOOL_DIAGNOSE,
    TOOL_ACTION: TOOL_PLAYBOOK,
}

MAX_LLM_STEPS = 5
"""一步对话最多 4 个工具：再多，端到端就没法在 8s 预算内落地。"""

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PLANNER_SYSTEM = """你是电商店铺诊断 Agent 的规划器。根据用户问题，从给定工具里挑出最少够用的调用序列。

规则：
1. 只能使用工具清单里的名字，不得发明新工具；
2. 参数只允许 window_days（1~30 的整数）与 as_of（YYYY-MM-DD 或 null），其它参数不要写；
3. 需要前置结果的工具（diagnose.decompose、playbook.search）必须排在 diagnose.run 之后；
4. 只问大盘/概况时只用 insights.overview；
5. 每个工具给一句中文 why，说明为什么需要它（会展示给用户，不超过 20 个字，不要复述用户问题）。

只输出 JSON，形如：
{"steps": [{"tool": "insights.overview", "args": {"window_days": 7, "as_of": null}, "why": "先确认大盘有没有异常"}]}"""


def _tool_help() -> str:
    return "\n".join(f"- {name}：{desc}" for name, desc in TOOL_CATALOG.items())


@dataclass(slots=True)
class PlanTrace:
    """计划器的一次决策记录，进 trace 用。"""

    planner: str
    ok: bool
    steps: list[str] = field(default_factory=list)
    reason: str | None = None
    dropped: list[str] = field(default_factory=list)
    raw: str | None = None
    elapsed_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "ok": self.ok,
            "steps": list(self.steps),
            "reason": self.reason,
            "dropped": list(self.dropped),
            "raw": (self.raw or "")[:500] or None,
            "elapsed_ms": self.elapsed_ms,
        }


class LLMPlanner:
    """让模型出计划，但**校验权在本地**。

    模型只负责「从白名单里挑哪几个工具、给什么参数」，落地的每个 Step 都过一遍
    `_validate()`：未知工具丢弃、参数越界夹紧、缺前置自动补、超过 5 步截断。
    任何一步出错（没 key、超时、JSON 坏、全被丢弃）都回退到 `RulePlanner`，
    并把原因留在 `last_trace` 里——计划器绝不能成为对话的单点故障。
    """

    name = "llm-v1"

    def __init__(self, llm: Any = None, fallback: RulePlanner | None = None) -> None:
        self._llm = llm
        self._fallback = fallback or RulePlanner()
        self.last_trace: PlanTrace = PlanTrace(planner=self.name, ok=False)

    # ---- 对外 ----

    def plan(self, intent: Intent) -> list[Step]:
        t0 = time.perf_counter()
        try:
            raw = self._ask(intent)
            steps, dropped = self._validate(raw, intent)
        except Exception as exc:  # noqa: BLE001 - 规划失败必须降级而不是中断对话
            steps, dropped = [], []
            self.last_trace = PlanTrace(
                planner=self.name,
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            return self._fallback.plan(intent)

        if not steps:
            self.last_trace = PlanTrace(
                planner=self.name,
                ok=False,
                reason="模型计划为空或全部不合法",
                dropped=dropped,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            return self._fallback.plan(intent)

        self.last_trace = PlanTrace(
            planner=self.name,
            ok=True,
            steps=[s.tool for s in steps],
            dropped=dropped,
            raw=json.dumps(raw, ensure_ascii=False),
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return steps

    # ---- 内部 ----

    def _ask(self, intent: Intent) -> dict[str, Any]:
        if self._llm is None:
            raise RuntimeError("LLMPlanner 未注入 llm 客户端")
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"可用工具：\n{_tool_help()}\n\n"
                    f"用户问题：{intent.question}\n"
                    f"初步识别的意图：{intent.kind}；时间窗：{intent.window_days} 天；"
                    f"as_of：{intent.as_of or '未指定（用默认诊断日）'}；"
                    f"款式：{intent.styles or '未指定'}；渠道：{intent.channels or '未指定'}；"
                    f"指标：{intent.metrics or '未指定'}"
                ),
            },
        ]
        # 700 而不是 400：四步计划的 why 一长就会被截断，截断后整份 JSON 不可解析，
        # 只能白白等 4s 再回退规则版（实测踩过）。
        body = self._llm.chat_json(messages, max_tokens=700)
        if not isinstance(body, dict):
            raise ValueError("计划器期望 JSON 对象")
        return body

    def _validate(self, body: dict[str, Any], intent: Intent) -> tuple[list[Step], list[str]]:
        raw_steps = body.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("计划缺少 steps 数组")

        dropped: list[str] = []
        ordered: list[Step] = []
        seen: set[str] = set()
        for item in raw_steps:
            if not isinstance(item, dict):
                dropped.append(repr(item)[:40])
                continue
            tool = str(item.get("tool") or "")
            if tool not in TOOL_CATALOG:
                dropped.append(tool or "(空工具名)")
                continue
            if tool in seen:
                continue
            seen.add(tool)
            ordered.append(Step(tool, self._clean_args(item.get("args"), intent), _clean_why(item.get("why"))))

        # 补前置：有 decompose/playbook 却没有 diagnose，就插在它前面
        for tool, dep in DEPENDS_ON.items():
            if any(s.tool == tool for s in ordered) and not any(s.tool == dep for s in ordered):
                idx = next(i for i, s in enumerate(ordered) if s.tool == tool)
                ordered.insert(idx, Step(dep, self._clean_args({}, intent), "补齐前置：先跑诊断再谈分解/动作"))
                seen.add(dep)

        # 大盘放最前：模型有时会把它排到后面，但它是最便宜的确认动作
        if any(s.tool == TOOL_OVERVIEW for s in ordered):
            ordered.sort(key=lambda s: 0 if s.tool == TOOL_OVERVIEW else 1)

        return ordered[:MAX_LLM_STEPS], dropped

    def _clean_args(self, args: Any, intent: Intent) -> dict[str, Any]:
        """只保留白名单参数，并夹紧取值；非法值静默忽略，用意图里的兜底。"""
        raw = args if isinstance(args, dict) else {}
        window = raw.get("window_days", intent.window_days)
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = intent.window_days
        window = max(1, min(30, window))

        as_of = raw.get("as_of", intent.as_of)
        if not isinstance(as_of, str) or not _AS_OF_RE.match(as_of.strip()):
            as_of = intent.as_of
        else:
            as_of = as_of.strip()
        return {"as_of": as_of, "window_days": window}


def _clean_why(why: Any, limit: int = 60) -> str:
    text = str(why or "").strip().replace("\n", " ")
    return text[:limit] or "（模型未说明理由）"


def make_planner(llm: Any = None) -> RulePlanner | LLMPlanner:
    """按生效配置挑计划器：有 key 且非 mock 就用 LLM，否则规则版。"""
    from app.core import runtime

    if llm is None and runtime.llm_active() != "real":
        return RulePlanner()
    if llm is None:
        try:
            from app.core.llm import get_client

            llm = get_client()
        except Exception:  # noqa: BLE001 - 网关没就绪就老实退回规则版
            return RulePlanner()
    return LLMPlanner(llm=llm)
