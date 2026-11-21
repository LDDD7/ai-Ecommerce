"""pytest 公共夹具。

注意：本机 %TEMP% 子目录 ACL 异常（见 docs/tech-compat.md），所以测试产物一律放
工作区 `.tmp/` 下，并在运行时把 TMPDIR 指过去。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / ".tmp"
TMP.mkdir(exist_ok=True)
os.environ.setdefault("TMPDIR", str(TMP))
os.environ.setdefault("TEMP", str(TMP))
os.environ.setdefault("TMP", str(TMP))

# 测试绝不打真模型：显式覆盖 .env 里的真 key，否则用例会依赖网络、额度和模型性情。
# 需要真链路的联调放在 tools/ 或 .tmp/ 的脚本里跑，不进 pytest。
os.environ["LLM_MODE"] = "mock"

# 测试默认关掉登录校验：其它 600+ 用例都是「已登录」前提下写的，
# 如果这里不短路，每个请求都会 401。登录/权限本身由 tests/test_auth.py 显式开启后测。
os.environ["AUTH_MODE"] = "off"

# 每个 pytest 进程用独立的库：并行跑测试（比如多 agent 同时跑）时不会互相改到对方的
# action_log / store_state，避免「单跑绿、并行红」的假失败。
TEST_DB = TMP / "pytest" / f"store_test_{os.getpid()}.db"


@pytest.fixture(scope="session")
def db_path() -> Path:
    TEST_DB.parent.mkdir(parents=True, exist_ok=True)
    if not TEST_DB.exists():
        from app.data.generator import build
        from datetime import date

        build(
            TEST_DB,
            sku_count=20,
            days=30,
            orders_target=3000,
            seed=7,
            end=date(2026, 11, 18),
            with_images=False,
        )
    return TEST_DB


@pytest.fixture(scope="session", autouse=True)
def _dispose_test_db(db_path: Path):
    yield
    # 尽力清理：Windows 上若还有连接没关（第三方库、失败的用例），删不掉也不该让整轮变红。
    for suffix in ("-wal", "-shm", ""):
        p = Path(str(db_path) + suffix)
        try:
            p.unlink()
        except (FileNotFoundError, PermissionError):
            pass


@pytest.fixture(scope="session", autouse=True)
def _settings_db(db_path: Path):
    os.environ["DB_PATH"] = str(db_path)
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
