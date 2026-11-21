"""促销日历（spec §28）。

系统里所有「促销」判断（`is_promo`、促销日占比、R7 大促透支、Critic 的「量价不可比」）
最终都读 `dim_date.is_promo`。种子数据里这个标记是生成器写的，但**导入的真实数据日期一律是 0**，
于是商家自己导进来的「双 11」在系统眼里只是普通的一天。

这里的做法：日历是唯一真源，`rebuild_flags()` 把它展开写回 `dim_date`。
"""

from __future__ import annotations

from app.promo.store import (
    KINDS,
    MAX_DAYS,
    adopt_existing,
    calendar_span,
    clear_range,
    create_period,
    delete_period,
    ensure_schema,
    get_period,
    list_periods,
    rebuild_flags,
    summary,
    update_period,
)

__all__ = [
    "KINDS",
    "MAX_DAYS",
    "adopt_existing",
    "calendar_span",
    "clear_range",
    "create_period",
    "delete_period",
    "ensure_schema",
    "get_period",
    "list_periods",
    "rebuild_flags",
    "summary",
    "update_period",
]
