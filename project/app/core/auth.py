"""登录与权限：账号、会话、角色。

设计约束（都是有意的）：

1. **密码只存 scrypt 哈希**（标准库 `hashlib.scrypt`，不引第三方依赖），比对用
   `hmac.compare_digest`，永不回明文。
2. **会话只存 token 的 sha256**。浏览器拿到的是 HttpOnly Cookie，前端 JS 读不到，
   也就不存在「存在 localStorage 里被 XSS 偷走」的问题。
3. **角色三档**：`staff` < `manager` < `owner`，数字越大权限越高；接口用
   `require_role("manager")` 声明「最低要哪一档」，越权一律 403。
4. `AUTH_MODE=off` 时整套逻辑短路成「匿名店主」，只用于本地调试与测试——
   测试环境若不短路，608 项既有用例会全部 401。

角色与动作的对应关系（与 spec §27 一致）：

| 角色 | 允许 |
|---|---|
| staff | 查数、诊断、问诊、起草动作 |
| manager | + 审批 risk ≤ 1、执行、驳回、回滚 |
| owner | + 审批 risk = 2、改设置（含 key）、数据导入/撤销、管理账号 |
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

Role = Literal["staff", "manager", "owner"]

# 越小越没权限；require_role 用 >= 判断。
ROLE_RANK: dict[str, int] = {"staff": 1, "manager": 2, "owner": 3}

COOKIE_NAME = "sd_session"

# 登录失败锁定：同一用户名 5 分钟内错 5 次，锁 5 分钟（内存态，单实例足够）。
LOCK_MAX_FAILS = 5
LOCK_WINDOW_S = 300.0

_FAILS: dict[str, list[float]] = {}

_USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'staff',
    password_hash TEXT NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    must_change   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_seen   TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class AuthError(ValueError):
    """可以原样展示给用户的登录错误（用户名或密码不对、被锁定、被禁用）。"""


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    username: str
    display_name: str
    role: str
    must_change: bool = False

    @property
    def name(self) -> str:
        """审计里显示的名字：优先昵称，其次用户名。"""
        return self.display_name.strip() or self.username

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "name": self.name,
            "role": self.role,
            "must_change": self.must_change,
        }


ANONYMOUS = User(user_id="u_anonymous", username="system", display_name="系统", role="owner")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: str | Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_USERS_DDL)


# ------------------------------------------------------------------ 密码


def hash_password(password: str) -> str:
    """scrypt 哈希，格式 `scrypt$n$r$p$salt$hash`（全十六进制，方便存 TEXT）。"""
    if len(password) < 6:
        raise AuthError("密码至少 6 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


# ------------------------------------------------------------------ 账号


def create_user(
    db_path: str | Path,
    username: str,
    password: str,
    *,
    role: str = "staff",
    display_name: str = "",
    must_change: bool = False,
) -> User:
    username = username.strip()
    if not username:
        raise AuthError("用户名不能为空")
    if role not in ROLE_RANK:
        raise AuthError(f"未知角色：{role}")
    ensure_schema(db_path)
    user_id = "u_" + secrets.token_hex(6)
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO users (user_id, username, display_name, role, password_hash,"
                " must_change, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    username,
                    display_name.strip(),
                    role,
                    hash_password(password),
                    1 if must_change else 0,
                    _now(),
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise AuthError(f"用户名「{username}」已经存在") from exc
    return User(user_id, username, display_name.strip(), role, must_change)


def list_users(db_path: str | Path) -> list[dict]:
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT user_id, username, display_name, role, disabled, must_change,"
            " created_at, last_login_at FROM users ORDER BY created_at"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["disabled"] = bool(item["disabled"])
        item["must_change"] = bool(item["must_change"])
        item["name"] = item["display_name"] or item["username"]
        out.append(item)
    return out


def update_user(
    db_path: str | Path,
    user_id: str,
    *,
    role: str | None = None,
    disabled: bool | None = None,
    password: str | None = None,
    display_name: str | None = None,
) -> dict:
    ensure_schema(db_path)
    sets: list[str] = []
    args: list[object] = []
    if role is not None:
        if role not in ROLE_RANK:
            raise AuthError(f"未知角色：{role}")
        sets.append("role = ?")
        args.append(role)
    if disabled is not None:
        sets.append("disabled = ?")
        args.append(1 if disabled else 0)
    if display_name is not None:
        sets.append("display_name = ?")
        args.append(display_name.strip())
    if password is not None:
        sets.append("password_hash = ?")
        args.append(hash_password(password))
        sets.append("must_change = 1")
    if not sets:
        raise AuthError("没有要修改的字段")
    args.append(user_id)
    with _connect(db_path) as conn:
        cur = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", args)
        if cur.rowcount == 0:
            raise LookupError("没有这个用户")
    return next(u for u in list_users(db_path) if u["user_id"] == user_id)


def count_users(db_path: str | Path) -> int:
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def bootstrap_admin(db_path: str | Path, password: str | None) -> dict | None:
    """首次启动没有账号时，建一个 owner。返回 `{username, password, generated}`。

    密码优先取 `AUTH_ADMIN_PASSWORD`；没配就随机生成 12 位并写 `.tmp/first-run.txt`，
    同时要求首次登录后必须改密（`must_change=1`）。
    """
    if count_users(db_path) > 0:
        return None
    generated = not password
    secret = password or secrets.token_urlsafe(9)
    create_user(db_path, "admin", secret, role="owner", display_name="店主", must_change=True)
    out = {"username": "admin", "password": secret, "generated": generated}
    if generated:
        root = Path(db_path).resolve().parent.parent
        target = root / ".tmp" / "first-run.txt"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "StoreDoctor 首次启动，已创建管理员账号：\n"
                f"  用户名：admin\n  密码：{secret}\n"
                "登录后请立即修改密码（界面会提示）。\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ 登录


def _locked(username: str) -> bool:
    now = time.time()
    hits = [t for t in _FAILS.get(username, []) if now - t < LOCK_WINDOW_S]
    _FAILS[username] = hits
    return len(hits) >= LOCK_MAX_FAILS


def _note_fail(username: str) -> None:
    _FAILS.setdefault(username, []).append(time.time())


def authenticate(db_path: str | Path, username: str, password: str) -> User:
    username = username.strip()
    if _locked(username):
        raise AuthError("登录失败次数过多，请 5 分钟后再试")
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, username, display_name, role, password_hash, disabled, must_change"
            " FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        _note_fail(username)
        raise AuthError("用户名或密码不正确")
    if row["disabled"]:
        raise AuthError("这个账号已被停用，请联系店主")
    _FAILS.pop(username, None)
    with _connect(db_path) as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE user_id = ?", (_now(), row["user_id"]))
    return User(
        row["user_id"],
        row["username"],
        row["display_name"],
        row["role"],
        bool(row["must_change"]),
    )


def change_password(db_path: str | Path, user_id: str, old: str, new: str) -> None:
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError("没有这个用户")
    if not verify_password(old, row["password_hash"]):
        raise AuthError("原密码不正确")
    if old == new:
        raise AuthError("新密码不能和原密码相同")
    stored = hash_password(new)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change = 0 WHERE user_id = ?",
            (stored, user_id),
        )


# ------------------------------------------------------------------ 会话


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(
    db_path: str | Path, user: User, *, ttl_hours: int = 168, user_agent: str = ""
) -> tuple[str, str]:
    ensure_schema(db_path)
    token = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen, user_agent)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_token_hash(token), user.user_id, _now(), exp, _now(), user_agent[:200]),
        )
        # 顺手清掉过期会话，表不会无限长
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    return token, exp


def resolve_session(db_path: str | Path, token: str) -> User | None:
    if not token:
        return None
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT s.user_id, s.expires_at, u.username, u.display_name, u.role, u.disabled,"
            " u.must_change FROM sessions s JOIN users u ON u.user_id = s.user_id"
            " WHERE s.token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None or row["expires_at"] < _now() or row["disabled"]:
            return None
        conn.execute(
            "UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (_now(), _token_hash(token))
        )
    return User(
        row["user_id"], row["username"], row["display_name"], row["role"], bool(row["must_change"])
    )


def end_session(db_path: str | Path, token: str) -> None:
    if not token:
        return
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def end_user_sessions(db_path: str | Path, user_id: str, *, keep: str | None = None) -> int:
    """改密/停用后踢掉该用户的所有会话；`keep` 是保留的当前 token。"""
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        if keep:
            cur = conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (user_id, _token_hash(keep)),
            )
        else:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return int(cur.rowcount)


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(minimum, 99)
