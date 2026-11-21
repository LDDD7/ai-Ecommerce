"""设置页的后端契约测试：key 只进不出、覆盖优先于 .env、保存后立刻生效。

这里刻意不碰网络：所有用例都在 mock 模式或注入假客户端下跑。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.core import runtime
from app.core.config import get_settings
from app.core.settings_store import (
    clear_all,
    mask,
    read_all,
    validate,
    write_many,
)

SECRET = "sk-test-1234567890abcdef"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def api():
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()

    async def _call(method: str, path: str, **kw):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            return await client.request(method, path, **kw)

    async def _run(method: str, path: str, **kw):
        return await _call(method, path, **kw)

    return lambda method, path, **kw: run(_run(method, path, **kw))


@pytest.fixture(autouse=True)
def _clean_store(db_path):
    """每个用例开始前清空覆盖值，避免用例之间互相污染。"""
    clear_all(db_path)
    runtime.refresh()
    yield
    clear_all(db_path)
    runtime.refresh()


# ---------------------------------------------------------------------------
# 掩码与校验（纯函数）
# ---------------------------------------------------------------------------


def test_mask_shows_head_and_tail_only():
    assert mask(SECRET) == "sk-tes…cdef"
    assert mask("short") == "*****"
    assert mask("") == ""
    assert mask(None) == ""


def test_validate_rejects_bad_scheme_and_mode():
    assert validate({"llm_base_url": "ftp://x"})
    assert validate({"llm_mode": "sometimes"})
    assert validate({"llm_model_fast": "has space"})
    assert validate({"llm_api_key": "short"})
    assert validate({"llm_base_url": "https://api.deepseek.com/v1"}) == []


def test_write_many_ignores_unknown_fields(db_path):
    write_many(db_path, {"llm_base_url": "https://a.example/v1", "evil_field": "x"})
    stored = read_all(db_path)
    assert stored["llm_base_url"] == "https://a.example/v1"
    assert "evil_field" not in stored


# ---------------------------------------------------------------------------
# 读接口：永不回明文
# ---------------------------------------------------------------------------


def test_get_masks_env_key_and_never_leaks(api, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    get_settings.cache_clear()
    runtime.refresh()

    resp = api("GET", "/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    text = json.dumps(body, ensure_ascii=False)

    assert SECRET not in text
    field = body["fields"]["llm_api_key"]
    assert field["secret"] is True
    assert field["value"] == ""
    assert field["masked"] == "sk-tes…cdef"
    assert field["has_value"] is True
    assert field["source"] == "env"


# ---------------------------------------------------------------------------
# 写接口：覆盖生效、清空回落、保存即可用
# ---------------------------------------------------------------------------


def test_put_settings_persists_and_flips_source(api, db_path):
    resp = api(
        "PUT",
        "/api/settings",
        json={
            "llm_base_url": "https://api.siliconflow.cn/v1",
            "llm_model_planner": "deepseek-ai/DeepSeek-V3",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fields"]["llm_base_url"]["source"] == "db"
    assert body["fields"]["llm_base_url"]["value"] == "https://api.siliconflow.cn/v1"
    assert body["llm"]["models"]["planner"] == "deepseek-ai/DeepSeek-V3"
    assert read_all(db_path)["llm_base_url"] == "https://api.siliconflow.cn/v1"


def test_empty_string_removes_override(api, db_path):
    api("PUT", "/api/settings", json={"llm_base_url": "https://a.example/v1"})
    assert read_all(db_path)["llm_base_url"]

    resp = api("PUT", "/api/settings", json={"llm_base_url": ""})
    assert resp.status_code == 200
    assert "llm_base_url" not in read_all(db_path)
    assert resp.json()["fields"]["llm_base_url"]["source"] in ("env", "none")


def test_put_rejects_bad_base_url(api):
    resp = api("PUT", "/api/settings", json={"llm_base_url": "file:///etc/passwd"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_setting"


def test_put_requires_at_least_one_field(api):
    resp = api("PUT", "/api/settings", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "empty_update"


def test_reset_back_to_env(api, db_path):
    api("PUT", "/api/settings", json={"llm_base_url": "https://a.example/v1"})
    resp = api("POST", "/api/settings/reset")
    assert resp.status_code == 200
    assert resp.json()["overrides"] == []
    assert read_all(db_path) == {}


# ---------------------------------------------------------------------------
# 运行时覆盖：保存后立刻影响 tl;dr 的判定
# ---------------------------------------------------------------------------


def test_override_switches_llm_active_without_restart(api):
    api("PUT", "/api/settings", json={"llm_mode": "real", "llm_api_key": SECRET})
    assert runtime.llm_active() == "real"
    assert runtime.value("llm_api_key") == SECRET

    api("PUT", "/api/settings", json={"llm_mode": "mock"})
    assert runtime.llm_active() == "mock"


def test_health_reflects_override(api):
    api("PUT", "/api/settings", json={"llm_mode": "real", "llm_api_key": SECRET})
    body = api("GET", "/api/health").json()
    assert body["llm"]["active"] == "real"
    assert body["llm"]["planner"] == "llm-v1"
    assert body["llm"]["configured"] is True


# ---------------------------------------------------------------------------
# 测试连接：没 key 时给可读原因，不抛 500
# ---------------------------------------------------------------------------


def test_test_endpoint_without_key_explains(api, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODE", "auto")
    get_settings.cache_clear()
    runtime.refresh()

    body = api("POST", "/api/settings/test", json={"target": "chat"}).json()
    assert body["ok"] is False
    assert "Key" in body["reason"]


def test_test_endpoint_rejects_bad_base_url(api):
    body = api(
        "POST",
        "/api/settings/test",
        json={"target": "chat", "api_key": SECRET, "base_url": "javascript:alert(1)"},
    ).json()
    assert body["ok"] is False
    assert "http" in body["reason"]
