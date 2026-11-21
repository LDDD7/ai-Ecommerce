"""工具注册表：编排层只认名字，不认实现，方便将来换成 MCP 风格的工具服务。

每个工具都是同步函数，内部直接调模块；编排层用 `anyio.to_thread` 把它们
丢到线程里跑，避免阻塞事件循环（SQLite 查询是同步的）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.diagnose import build_diagnosis
from app.insights import build_overview
from app.playbook.search import ACTION_NAMES, search_for_codes

# 一轮对话里最多给几张诊断卡配动作：再多，报告就变成策略卡说明书了。
PLAYBOOK_CARDS = 5
# 每张卡给几条动作：2 条足够做「首选 + 备选」，也方便人审时二选一。
PLAYBOOK_ACTIONS_PER_CARD = 2


@dataclass(slots=True)
class ToolResult:
    name: str
    ok: bool
    payload: dict[str, Any]
    summary: str = ""
    error: str | None = None


def _overview(db_path: str, as_of: str | None, window_days: int, **_: Any) -> ToolResult:
    body = build_overview(db_path, window_days=window_days, as_of=as_of)
    kpis = {k["name"]: k for k in body["kpis"]}
    gmv = kpis.get("gmv", {})
    summary = (
        f"诊断日 {body['as_of']}：成交额 {gmv.get('value', 0):,.0f} 元"
        f"（{gmv.get('delta_pct', 0):+.1f}%），健康分 {body['health']['score']}（{body['health']['grade']}）"
    )
    return ToolResult("insights.overview", True, body, summary)


def _diagnose(db_path: str, as_of: str | None, window_days: int, **_: Any) -> ToolResult:
    body = build_diagnosis(db_path, as_of=as_of, window_days=window_days)
    n = len(body["cards"])
    codes = "、".join(sorted({c["code"] for c in body["cards"]})) or "无"
    summary = f"扫描 {body['meta']['points']} 个异常点，出 {n} 张诊断卡（根因：{codes}）"
    return ToolResult("diagnose.run", True, body, summary)


def _decompose(db_path: str, as_of: str | None, window_days: int, **_: Any) -> ToolResult:
    # 分解结果已包含在 diagnose 里，这里单独再算一次是为了让计划看起来完整、
    # 也方便将来「只问成交额构成」时不必跑整条诊断。
    body = build_diagnosis(db_path, as_of=as_of, window_days=window_days)
    gmv = body["gmv"]
    return ToolResult("diagnose.decompose", True, gmv, gmv.get("summary", ""))


def _playbook(
    db_path: str,
    as_of: str | None,
    window_days: int,
    *,
    cards: list[dict[str, Any]] | None = None,
    **_: Any,
) -> ToolResult:
    """给每张诊断卡配可执行动作。

    走 `search_for_codes`（按根因直达）而不是自然语言召回：诊断卡已经给了 R1..R10，
    再让检索去猜 query 措辞只会引入不确定性。自然语言那一路留给 `/api/playbook/search`。
    """
    if not cards:
        return ToolResult(
            "playbook.search",
            False,
            {},
            error="playbook.search 依赖诊断结果，需要先运行 diagnose.run",
        )

    items: list[dict[str, Any]] = []
    total = 0
    for card in cards[:PLAYBOOK_CARDS]:
        hits, _ = search_for_codes(
            db_path,
            [card["code"]],
            risk_max=2,
            limit=PLAYBOOK_ACTIONS_PER_CARD,
        )
        actions: list[dict[str, Any]] = []
        for hit in hits:
            action = hit.as_dict()
            action["action_label"] = ACTION_NAMES.get(action["action_type"], action["action_type"])
            actions.append(action)
        total += len(actions)
        items.append(
            {
                "card_id": card["card_id"],
                "code": card["code"],
                "title": card["title"],
                "gmv_impact": card.get("gmv_impact"),
                "actions": actions,
            }
        )

    payload = {
        "items": items,
        "meta": {
            "cards": len(items),
            "actions": total,
            "engine": "by_root",
            "risk_max": 2,
            "cards_without_action": sum(1 for i in items if not i["actions"]),
        },
    }
    summary = f"{len(items)} 张诊断卡共 {total} 条建议动作"
    return ToolResult("playbook.search", True, payload, summary)


def _action(
    db_path: str,
    as_of: str | None,
    window_days: int,
    *,
    cards: list[dict[str, Any]] | None = None,
    playbook: dict[str, Any] | None = None,
    store_id: str = "store_001",
    trace_id: str = "",
    **_: Any,
) -> ToolResult:
    """把「诊断卡 + 策略卡动作」落成可审批的动作草案。

    只起草，不执行；风险等级由 `app.actions.schema.classify_risk` 判定，
    到没到人审门槛在落库那一刻就已经定了。
    """
    if not cards:
        return ToolResult(
            "action.draft",
            False,
            {},
            error="action.draft 依赖诊断结果，需要先运行 diagnose.run",
        )
    try:  # 懒 import：草案生成器是可选项，缺了不该让整条对话挂掉
        from app.actions.plan import draft_actions
    except Exception as exc:  # noqa: BLE001
        return ToolResult("action.draft", False, {}, error=f"动作草案生成器未就绪：{type(exc).__name__}: {exc}")

    out = draft_actions(
        db_path,
        cards,
        playbook or {},
        store_id=store_id,
        trace_id=trace_id,
        as_of=as_of,
    )
    actions = out.get("actions", [])
    tally = {0: 0, 1: 0, 2: 0}
    for item in actions:
        level = int(item.get("risk_level", 1))
        tally[level] = tally.get(level, 0) + 1
    summary = (
        f"起草 {len(actions)} 条动作（低风险 {tally[0]} / 需人审 {tally[1]} / 高风险 {tally[2]}）"
        if actions
        else "没有可起草的动作（诊断卡未配到策略动作或参数取不到）"
    )
    return ToolResult("action.draft", True, {"items": actions, "meta": out.get("meta", {})}, summary)


TOOLS: dict[str, Callable[..., ToolResult]] = {
    "insights.overview": _overview,
    "diagnose.run": _diagnose,
    "diagnose.decompose": _decompose,
    "playbook.search": _playbook,
    "action.draft": _action,
}


def call_tool(
    name: str,
    db_path: str,
    as_of: str | None = None,
    window_days: int = 7,
    **kwargs: Any,
) -> ToolResult:
    fn = TOOLS.get(name)
    if fn is None:
        return ToolResult(name, False, {}, error=f"未注册的工具：{name}")
    try:
        return fn(db_path, as_of=as_of, window_days=window_days, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 工具失败要变成事件而不是 500
        return ToolResult(name, False, {}, error=f"{type(exc).__name__}: {exc}")
