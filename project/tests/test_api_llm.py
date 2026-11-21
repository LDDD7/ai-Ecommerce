"""LLM 相关 HTTP 接口：健康检查字段与探活接口在无 key 时的降级。"""

from __future__ import annotations

import asyncio
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.main import app


def call(method: str, path: str, json: Any = None):
    async def _run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=json)

    return asyncio.run(_run())


def test_health_llm_block():
    body = call("GET", "/api/health").json()
    llm = body["llm"]
    assert llm["active"] in ("real", "mock")
    assert llm["planner"] in ("llm-v1", "rule-v1")
    assert isinstance(llm["configured"], bool)
    # 两者必须自洽：real 才能用 llm-v1
    assert (llm["planner"] == "llm-v1") == (llm["active"] == "real")


def test_llm_probe_degrades_to_mock(monkeypatch):
    from app.core import config

    monkeypatch.setenv("LLM_MODE", "mock")
    config.get_settings.cache_clear()
    try:
        r = call("GET", "/api/llm/probe")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["active"] == "mock"
        assert "规则版" in body["reason"]
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()
