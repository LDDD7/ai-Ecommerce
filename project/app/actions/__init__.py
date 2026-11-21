"""执行闭环：动作 schema、草案生成（plan）、落库与状态机（store）、mock 执行（execute）。

对外只暴露最常用的入口，避免调用方到处 import 子模块：

    from app.actions import create_action, execute_action, draft_actions
"""

from __future__ import annotations

from app.actions.execute import apply as apply_action
from app.actions.execute import preview as preview_action
from app.actions.execute import read_state
from app.actions.execute import revert as revert_action
from app.actions.schema import (
    ACTION_TYPES,
    RISK_AUTO,
    RISK_LABELS,
    RISK_MANAGER,
    RISK_REVIEW,
    SPECS,
    ActionError,
    action_summary,
    check_guardrails,
    classify_risk,
    idempotency_key,
    validate_params,
)
from app.actions.store import (
    approve_action,
    audit_events,
    create_action,
    execute_action,
    get_action,
    list_actions,
    action_counts,
    reject_action,
    rollback_action,
    trace_events,
)

__all__ = [
    "ACTION_TYPES",
    "RISK_AUTO",
    "RISK_LABELS",
    "RISK_MANAGER",
    "RISK_REVIEW",
    "SPECS",
    "ActionError",
    "action_summary",
    "apply_action",
    "approve_action",
    "audit_events",
    "check_guardrails",
    "classify_risk",
    "create_action",
    "execute_action",
    "get_action",
    "idempotency_key",
    "list_actions",
    "action_counts",
    "preview_action",
    "read_state",
    "reject_action",
    "revert_action",
    "rollback_action",
    "trace_events",
    "validate_params",
]
