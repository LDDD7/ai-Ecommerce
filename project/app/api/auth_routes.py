"""登录相关接口（spec §27）。

- `POST /auth/login`   用户名 + 密码 → 下发 HttpOnly Cookie
- `POST /auth/logout`  清 Cookie + 删会话
- `GET  /auth/me`      当前登录人（前端启动时调，401 就跳登录页）
- `POST /auth/password` 改自己的密码（改完把其它会话踢掉）
- `GET/POST /auth/users`、`PATCH /auth/users/{id}` 店主管理账号

错误约定：401 `unauthorized`、403 `forbidden`、400 `invalid_credentials` / `weak_password`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core import auth
from app.core.config import get_settings
from app.core.guards import actor_of, current_user, require_role

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class PasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=200)
    role: str = "staff"
    display_name: str = ""


class UserPatchRequest(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=6, max_length=200)
    display_name: str | None = None


def _db() -> str:
    return get_settings().db_path


def _set_cookie(response: Response, token: str, ttl_hours: int) -> None:
    settings = get_settings()
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.post("/auth/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    auth.bootstrap_admin(settings.db_path, settings.auth_admin_password or None)
    try:
        user = auth.authenticate(settings.db_path, req.username, req.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    token, expires_at = auth.start_session(
        settings.db_path,
        user,
        ttl_hours=settings.session_ttl_hours,
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_cookie(response, token, settings.session_ttl_hours)
    return {"user": user.as_dict(), "expires_at": expires_at, "auth_mode": settings.auth_mode}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get(auth.COOKIE_NAME, "")
    auth.end_session(_db(), token)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not isinstance(user, auth.User):
        raise HTTPException(status_code=401, detail="请先登录")
    return {"user": user.as_dict(), "auth_mode": get_settings().auth_mode}


@router.post("/auth/password")
def change_password(req: PasswordRequest, request: Request, response: Response) -> dict[str, Any]:
    user = current_user(request)
    settings = get_settings()
    try:
        auth.change_password(settings.db_path, user.user_id, req.old_password, req.new_password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current = request.cookies.get(auth.COOKIE_NAME, "")
    auth.end_user_sessions(settings.db_path, user.user_id, keep=current)
    token, expires_at = auth.start_session(
        settings.db_path,
        auth.User(user.user_id, user.username, user.display_name, user.role, False),
        ttl_hours=settings.session_ttl_hours,
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_cookie(response, token, settings.session_ttl_hours)
    return {"ok": True, "expires_at": expires_at}


@router.get("/auth/users")
def list_users(_: auth.User = Depends(require_role("owner"))) -> dict[str, Any]:
    return {"items": auth.list_users(_db())}


@router.post("/auth/users")
def create_user(req: UserCreateRequest, _: auth.User = Depends(require_role("owner"))) -> dict[str, Any]:
    try:
        user = auth.create_user(
            _db(),
            req.username,
            req.password,
            role=req.role,
            display_name=req.display_name,
            must_change=True,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user.as_dict()}


@router.patch("/auth/users/{user_id}")
def patch_user(
    user_id: str, req: UserPatchRequest, _: auth.User = Depends(require_role("owner"))
) -> dict[str, Any]:
    try:
        item = auth.update_user(
            _db(),
            user_id,
            role=req.role,
            disabled=req.disabled,
            password=req.password,
            display_name=req.display_name,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if req.disabled or req.password:
        auth.end_user_sessions(_db(), user_id)
    return {"user": item}


@router.get("/auth/whoami")
def whoami(request: Request) -> dict[str, Any]:
    """给审计/联调用：不需要登录也能看到「系统当前认为你是谁」（off 模式返回匿名）。"""
    user = getattr(request.state, "user", None)
    if not isinstance(user, auth.User):
        return {"user": None, "actor": actor_of(request), "auth_mode": get_settings().auth_mode}
    return {"user": user.as_dict(), "actor": actor_of(request), "auth_mode": get_settings().auth_mode}
