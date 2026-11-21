"""登录与权限的接口测试。

其余 600+ 用例都在 `AUTH_MODE=off` 下跑（见 conftest），这里显式打开鉴权，
用真实 Cookie 走一遍：登录、改密、越权、锁定、过期、退出。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import auth

ADMIN_PASSWORD = "secret123"


@pytest.fixture
def auth_client(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """打开鉴权的 app：首次启动会用 AUTH_ADMIN_PASSWORD 建 admin。

    每个用例先把 users/sessions 清空，否则「改密」用例会把密码留给下一个用例。
    动作/订单数据不动，它们与本文件要测的东西无关。
    """
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("DB_PATH", str(db_path))
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE IF EXISTS sessions")
        conn.execute("DROP TABLE IF EXISTS users")
    auth._FAILS.clear()
    from app.core import config

    config.get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client
    auth._FAILS.clear()
    config.get_settings.cache_clear()


def _login(client: TestClient, username: str = "admin", password: str = ADMIN_PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ------------------------------------------------------------------ 密码哈希


def test_password_hash_roundtrip() -> None:
    stored = auth.hash_password("hunter22")
    assert stored.startswith("scrypt$")
    assert "hunter22" not in stored
    assert auth.verify_password("hunter22", stored)
    assert not auth.verify_password("hunter23", stored)
    assert not auth.verify_password("hunter22", "garbage")


def test_short_password_rejected() -> None:
    with pytest.raises(auth.AuthError):
        auth.hash_password("123")


# ------------------------------------------------------------------ 未登录


def test_api_requires_login(auth_client: TestClient) -> None:
    res = auth_client.get("/api/overview")
    assert res.status_code == 401
    assert res.json()["error"] == "unauthorized"


def test_health_is_public(auth_client: TestClient) -> None:
    res = auth_client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] in {"ok", "degraded"}


# ------------------------------------------------------------------ 登录 / 登出


def test_login_wrong_password(auth_client: TestClient) -> None:
    res = _login(auth_client, password="nope-nope")
    assert res.status_code == 401
    assert "不正确" in res.json()["detail"]


def test_login_success_sets_cookie_and_me(auth_client: TestClient) -> None:
    res = _login(auth_client)
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == "owner"
    assert body["user"]["must_change"] is True
    assert auth.COOKIE_NAME in auth_client.cookies

    me = auth_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin"


def test_logout_kills_session(auth_client: TestClient) -> None:
    assert _login(auth_client).status_code == 200
    assert auth_client.post("/api/auth/logout").status_code == 200
    assert auth_client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected(auth_client: TestClient, db_path: Path) -> None:
    assert _login(auth_client).status_code == 200
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE sessions SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    assert auth_client.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------ 改密


def test_change_password_flow(auth_client: TestClient) -> None:
    assert _login(auth_client).status_code == 200
    bad = auth_client.post(
        "/api/auth/password", json={"old_password": "wrong-one", "new_password": "newpass123"}
    )
    assert bad.status_code == 400
    assert "原密码" in bad.json()["detail"]

    ok = auth_client.post(
        "/api/auth/password", json={"old_password": ADMIN_PASSWORD, "new_password": "newpass123"}
    )
    assert ok.status_code == 200
    me = auth_client.get("/api/auth/me").json()["user"]
    assert me["must_change"] is False

    # 旧密码失效、新密码可用
    assert _login(auth_client, password=ADMIN_PASSWORD).status_code == 401
    assert _login(auth_client, password="newpass123").status_code == 200


# ------------------------------------------------------------------ 角色


def test_role_gates(auth_client: TestClient, db_path: Path) -> None:
    assert _login(auth_client).status_code == 200
    created = auth_client.post(
        "/api/auth/users",
        json={"username": "xiaowang", "password": "wang12345", "role": "staff", "display_name": "小王"},
    )
    assert created.status_code == 200
    assert created.json()["user"]["role"] == "staff"

    staff = TestClient(auth_client.app)
    res = staff.post("/api/auth/login", json={"username": "xiaowang", "password": "wang12345"})
    assert res.status_code == 200
    assert res.json()["user"]["must_change"] is True

    # 读接口能用
    assert staff.get("/api/overview").status_code == 200
    # 写设置 / 审批 / 导入：403
    assert staff.get("/api/settings").status_code == 403
    assert staff.post("/api/ingest/preview", files={"file": ("a.csv", b"x")}).status_code == 403
    assert staff.post("/api/actions/nope/approve", json={"approver": "x"}).status_code == 403

    # 店主能用
    assert auth_client.get("/api/settings").status_code == 200


def test_only_owner_manages_users(auth_client: TestClient) -> None:
    assert _login(auth_client).status_code == 200
    auth_client.post(
        "/api/auth/users",
        json={"username": "manager1", "password": "mgr12345", "role": "manager"},
    )
    mgr = TestClient(auth_client.app)
    mgr.post("/api/auth/login", json={"username": "manager1", "password": "mgr12345"})
    assert mgr.get("/api/auth/users").status_code == 403
    assert mgr.post("/api/auth/users", json={"username": "x", "password": "xxxxxx"}).status_code == 403


def test_login_lockout(auth_client: TestClient) -> None:
    auth._FAILS.clear()
    for _ in range(auth.LOCK_MAX_FAILS):
        assert _login(auth_client, password="bad-pass").status_code == 401
    locked = _login(auth_client, password="bad-pass")
    assert locked.status_code == 401
    assert "次数过多" in locked.json()["detail"]
    auth._FAILS.clear()


def test_disabled_user_cannot_login(auth_client: TestClient) -> None:
    assert _login(auth_client).status_code == 200
    users = auth_client.get("/api/auth/users").json()["items"]
    uid = next(u["user_id"] for u in users if u["username"] == "admin")
    auth_client.post("/api/auth/users", json={"username": "temp", "password": "temp1234"})
    temp = next(u for u in auth_client.get("/api/auth/users").json()["items"] if u["username"] == "temp")
    patched = auth_client.patch(f"/api/auth/users/{temp['user_id']}", json={"disabled": True})
    assert patched.status_code == 200
    assert _login(auth_client, "temp", "temp1234").status_code == 401
    assert uid  # admin 仍在
