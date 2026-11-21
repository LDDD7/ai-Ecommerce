"""FastAPI 依赖：取当前登录用户、按最低角色拦截、给审计取操作人。

中间件（`app/main.py`）已经做了「必须登录」的粗筛，这里只处理两件事：

1. 需要登录但中间件没覆盖到的场景（比如直接给路由挂依赖）；
2. **角色门槛**——`Depends(require_role("manager"))`，不够就 403。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.core import auth
from app.core.config import get_settings


def current_user(request: Request) -> auth.User:
    user = getattr(request.state, "user", None)
    if isinstance(user, auth.User):
        return user
    raise HTTPException(status_code=401, detail="请先登录")


def require_role(minimum: str):
    """返回一个依赖：当前用户角色必须 ≥ minimum。"""

    def _dep(request: Request) -> auth.User:
        user = current_user(request)
        if not auth.role_at_least(user.role, minimum):
            raise HTTPException(
                status_code=403,
                detail=f"权限不足：这一步需要「{_ROLE_CN.get(minimum, minimum)}」及以上",
            )
        return user

    return _dep


_ROLE_CN = {"staff": "运营", "manager": "店长", "owner": "店主"}


def actor_of(request: Request, fallback: str = "店长") -> str:
    """审计操作人：登录用户的显示名；未登录（AUTH_MODE=off）时用 fallback。"""
    user = getattr(request.state, "user", None)
    if isinstance(user, auth.User) and user.user_id != auth.ANONYMOUS.user_id:
        return user.name
    return fallback


def auth_enabled() -> bool:
    return get_settings().auth_mode == "password"
