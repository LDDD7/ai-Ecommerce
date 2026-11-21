"""FastAPI 入口：/api/* 路由 + 前端静态资源（构建产物存在时）。

启动：
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.actions_routes import router as actions_router
from app.api.auth_routes import router as auth_router
from app.api.creative_routes import router as creative_router
from app.api.promo_routes import router as promo_router
from app.api.routes import router
from app.core import auth
from app.core.config import get_settings
from app.ingest.api import router as ingest_router

BASE_DIR = Path(__file__).resolve().parents[1]

# 不需要登录的接口：健康检查（部署探活）和登录本身。
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/whoami"})

# 严格校验报错时，点名「这个位置到底允许哪些字段」，省得调用方猜。
# 键是请求体里的路径前缀（错误 loc 去掉最后一个字段）。
_ALLOWED_FIELDS = {
    "body.spec": "metrics / dimensions / time / filters / order_by / limit",
    "body.spec.time": "start / end",
    "body.spec.filters": "field / op / value",
    "body.spec.order_by": "field / dir",
}


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # 首次启动且没有任何账号时，建一个店主账号：密码取自 AUTH_ADMIN_PASSWORD，
        # 没配就随机生成并打印（只打印这一次，同时写进 .tmp/first-run.txt）。
        if get_settings().auth_mode == "password":
            try:
                created = auth.bootstrap_admin(
                    get_settings().db_path, get_settings().auth_admin_password or None
                )
            except Exception as exc:  # 账号建不起来不应该拖垮整个服务
                print(f"[auth] 初始化管理员失败：{exc}", flush=True)
            else:
                if created is not None:
                    print(
                        f"[auth] 首次启动，已创建管理员：{created['username']} / {created['password']}"
                        "（登录后请立即改密）",
                        flush=True,
                    )
        yield

    app = FastAPI(title="StoreDoctor 店诊 Agent", version="0.1.0", lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _require_login(request: Request, call_next):
        """登录粗筛：/api/* 除白名单外一律要会话；AUTH_MODE=off 时当作匿名店主。"""
        conf = get_settings()
        if conf.auth_mode != "password":
            request.state.user = auth.ANONYMOUS
            return await call_next(request)
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith("/api") or path in PUBLIC_PATHS:
            request.state.user = None
            return await call_next(request)
        user = auth.resolve_session(conf.db_path, request.cookies.get(auth.COOKIE_NAME, ""))
        if user is None:
            return JSONResponse(
                status_code=401, content={"error": "unauthorized", "message": "请先登录"}
            )
        request.state.user = user
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # 统一 400 + 可读原因，前端好展示。
        # extra_forbidden 单独翻成人话：QuerySpec 多写的字段会让调用方以为查对了，
        # 所以这里必须点名「未知字段」并列出可用字段。
        errors = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []))
            msg = e.get("msg", "")
            if e.get("type") == "extra_forbidden":
                field = str(e["loc"][-1]) if e.get("loc") else ""
                allowed = _ALLOWED_FIELDS.get(loc.rsplit(".", 1)[0] if "." in loc else "")
                msg = f"未知字段「{field}」"
                if allowed:
                    msg += f"；只接受 {allowed}"
            errors.append({"loc": loc, "msg": msg})
        return JSONResponse(status_code=400, content={"error": "invalid_spec", "detail": errors})

    app.include_router(router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(actions_router, prefix="/api")
    app.include_router(creative_router, prefix="/api")
    app.include_router(ingest_router, prefix="/api")
    app.include_router(promo_router, prefix="/api")

    dist = BASE_DIR / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")

    return app


app = create_app()
