"""动作闭环的 HTTP 接口（spec §9 / §13）。

和诊断接口一样，这一层只做「参数校验 + 错误码映射 + 计时」，业务全在
`app.actions.store` / `app.actions.execute` / `app.actions.plan` 里。

错误码约定（与 spec §13 一致）：
- 400 `invalid_action`：参数/状态非法（`ActionError`）
- 404 `action_not_found`：动作不存在（`LookupError`）
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.actions import store as action_store
from app.actions.execute import preview as preview_action
from app.actions.schema import ACTION_TYPES, ActionError, classify_risk, spec_of, validate_params
from app.core import auth
from app.core.config import get_settings
from app.core.guards import actor_of, require_role

router = APIRouter()


class ActionDryRunRequest(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    sku_id: str | None = None
    store_id: str | None = None


class ActionCreateRequest(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    sku_id: str | None = None
    store_id: str | None = None
    trace_id: str = ""
    reason: str = ""
    day: str | None = None


class ActionApproveRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=64)
    role: str = "manager"
    note: str = ""


class ActionActorRequest(BaseModel):
    actor: str = Field(default="operator", max_length=64)
    reason: str = ""


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _db() -> str:
    return get_settings().db_path


@router.get("/actions/types")
def action_types() -> dict[str, Any]:
    """动作类型目录：前端用它渲染下拉与参数表单。"""
    out = []
    for name in ACTION_TYPES:
        spec = spec_of(name)
        out.append(
            {
                "type": spec.type,
                "label": spec.label,
                "required": list(spec.required),
                "optional": list(spec.optional),
                "effect_metric": spec.effect_metric,
                "store_field": spec.store_field,
                "description": spec.description,
                "risk_note": spec.risk_note,
            }
        )
    return {"types": out, "risk_labels": {"0": "低风险", "1": "需人审", "2": "高风险·必须人审"}}


@router.post("/actions/dry-run")
def action_dry_run(req: ActionDryRunRequest) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        spec_of(req.type)
        clean = validate_params(req.type, req.params)
        action = {"type": req.type, "params": clean, "sku_id": req.sku_id, "store_id": req.store_id}
        out = preview_action(_db(), action)
        price = out.get("before")
        try:
            price_f = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price_f = None
        out["risk_level"] = classify_risk(req.type, clean, current_price=price_f)
    except ActionError as exc:
        raise _fail(exc) from exc
    out["meta"] = {"wall_ms": round((time.perf_counter() - t0) * 1000, 1)}
    return out


@router.post("/actions")
def action_create(req: ActionCreateRequest, request: Request) -> dict[str, Any]:
    try:
        return action_store.create_action(
            _db(),
            type=req.type,
            params=req.params,
            sku_id=req.sku_id,
            store_id=req.store_id,
            trace_id=req.trace_id,
            reason=req.reason,
            actor=actor_of(request, fallback="系统"),
            day=req.day,
        )
    except ActionError as exc:
        raise _fail(exc) from exc


@router.post("/actions/draft")
def action_draft(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """从诊断结果直接起草动作（演示用）：body = {"cards": [...], "playbook": {...}}。

    真实链路里这一步由编排层的 `action.draft` 工具完成；这个接口方便前端/脚本单独调。
    """
    cards = payload.get("cards") or []
    playbook = payload.get("playbook") or None
    if not isinstance(cards, list) or not cards:
        raise HTTPException(status_code=400, detail="cards 不能为空（先跑 /api/diagnose）")
    from app.actions import plan as action_plan  # 懒 import：草案生成依赖诊断/策略库，晚加载更省启动时间

    try:
        return action_plan.draft_actions(
            _db(),
            cards,
            playbook,
            store_id=payload.get("store_id"),
            trace_id=payload.get("trace_id") or "",
            as_of=payload.get("as_of"),
            actor=actor_of(request, fallback=payload.get("actor") or "智能助手"),
        )
    except ActionError as exc:
        raise _fail(exc) from exc


@router.get("/actions")
def action_list(
    status: str | None = None,
    type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    items = action_store.list_actions(_db(), status=status, type=type, limit=limit)
    # 计数不受 limit 截断；status_counts 跟随 type 过滤，total 是全量口径
    counts = action_store.action_counts(_db(), type=type)
    all_counts = action_store.action_counts(_db())
    total = sum(all_counts.values())
    count = counts.get(status, 0) if status else sum(counts.values())
    return {"items": items, "count": count, "total": total, "status_counts": counts}


@router.get("/actions/{action_id}")
def action_get(action_id: str) -> dict[str, Any]:
    row = action_store.get_action(_db(), action_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"动作不存在：{action_id}")
    return row


@router.post("/actions/{action_id}/approve")
def action_approve(
    action_id: str,
    req: ActionApproveRequest,
    request: Request,
    user: auth.User = Depends(require_role("manager")),
) -> dict[str, Any]:
    # 登录后以会话为准：谁点的审批就记谁，角色也用真实角色，前端传什么都不算数。
    approver = actor_of(request, fallback=req.approver)
    role = user.role if user.user_id != auth.ANONYMOUS.user_id else req.role
    try:
        return action_store.approve_action(_db(), action_id, approver=approver, role=role, note=req.note)
    except (ActionError, LookupError) as exc:
        raise _fail(exc) from exc


@router.post("/actions/{action_id}/reject")
def action_reject(
    action_id: str,
    req: ActionActorRequest,
    request: Request,
    _: auth.User = Depends(require_role("manager")),
) -> dict[str, Any]:
    try:
        return action_store.reject_action(
            _db(), action_id, actor=actor_of(request, fallback=req.actor), reason=req.reason
        )
    except (ActionError, LookupError) as exc:
        raise _fail(exc) from exc


@router.post("/actions/{action_id}/execute")
def action_execute(
    action_id: str,
    req: ActionActorRequest,
    request: Request,
    _: auth.User = Depends(require_role("manager")),
) -> dict[str, Any]:
    try:
        return action_store.execute_action(
            _db(), action_id, actor=actor_of(request, fallback=req.actor)
        )
    except (ActionError, LookupError) as exc:
        raise _fail(exc) from exc


@router.post("/actions/{action_id}/rollback")
def action_rollback(
    action_id: str,
    req: ActionActorRequest,
    request: Request,
    _: auth.User = Depends(require_role("manager")),
) -> dict[str, Any]:
    try:
        return action_store.rollback_action(
            _db(), action_id, actor=actor_of(request, fallback=req.actor), reason=req.reason
        )
    except (ActionError, LookupError) as exc:
        raise _fail(exc) from exc


@router.get("/audit")
def audit(action_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    items = action_store.audit_events(_db(), action_id=action_id, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/trace/{trace_id}")
def trace(trace_id: str) -> dict[str, Any]:
    actions = [a for a in action_store.list_actions(_db(), limit=200) if a.get("trace_id") == trace_id]
    events = action_store.trace_events(_db(), trace_id)
    if not actions and not events:
        raise HTTPException(status_code=404, detail=f"没有这个 trace：{trace_id}")
    return {"trace_id": trace_id, "actions": actions, "events": events, "count": len(events)}
