"""`/api/playbook/search` 接口测试：按根因直达、自然语言召回、参数校验与错误分支。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.playbook import search as search_mod
from app.retrieval.hybrid import Hit
from app.retrieval.texts import Doc


def call(method: str, path: str, json: Any = None):
    async def _run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=json)

    return asyncio.run(_run())


def _fake_hit(card_id: str, title: str, roots: list[str], rrf: float = 0.03) -> Hit:
    doc = Doc(
        "playbook",
        card_id,
        title,
        f"{title}\n适用根因：" + "、".join(roots),
        list(roots),
        {"risk": 1, "scope": ["sku"], "action": "swap_creative", "metrics": ["refund_rate"], "root_causes": roots},
    )
    return Hit(doc=doc, rrf=rrf, fts_rank=1, vec_rank=None)


def test_search_by_root_codes_without_query():
    """诊断卡拿到 R4 之后直接按码取卡，不需要 DB 索引。"""
    r = call("POST", "/api/playbook/search", {"root_causes": ["R4"]})
    assert r.status_code == 200
    body = r.json()
    ids = [hit["card_id"] for hit in body["hits"]]
    assert "PB-R4-01" in ids
    assert body["meta"]["engine"] == "by_root"
    assert body["meta"]["returned"] == len(body["hits"]) >= 1
    assert body["meta"]["wall_ms"] >= 0


def test_search_by_unknown_code_is_soft_failure():
    r = call("POST", "/api/playbook/search", {"root_causes": ["R99"]})
    assert r.status_code == 200
    body = r.json()
    assert body["hits"] == []
    assert body["meta"]["unknown_codes"] == ["R99"]
    assert "人工介入" in body["meta"]["note"]


def test_search_natural_language_uses_recall(monkeypatch):
    """自然语言走混合召回 + 重排；这里 stube 掉召回，只验接口链路。"""

    def fake_search(db_path, query, docs=None, owner_table=None, limit=12, with_vec=None):
        assert owner_table == "playbook"
        return (
            [
                _fake_hit("PB-R4-01", "补齐尺码表与试穿建议", ["R4"], rrf=0.0328),
                _fake_hit("PB-R2-02", "三图轮测，48 小时淘汰制", ["R2"], rrf=0.0161),
            ],
            {"fts": 2, "vec": 0, "vec_enabled": False, "degraded": True},
        )

    monkeypatch.setattr(search_mod, "hybrid_search", fake_search)
    r = call(
        "POST",
        "/api/playbook/search",
        {
            "query": "退款率上升 尺码偏小 详情页没有尺码表",
            "root_causes": ["R4"],
            "metrics": ["refund_rate"],
            "limit": 2,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["hits"], "召回被 stub 成两条，应该返回结果"
    assert body["hits"][0]["card_id"] == "PB-R4-01"
    assert body["meta"]["recall"] == 2
    assert body["hits"][0]["reasons"]
    assert "expected_effect" in body["hits"][0]


def test_risk_filter_applies():
    r = call("POST", "/api/playbook/search", {"root_causes": ["R9"], "risk_max": 0})
    assert r.status_code == 200
    body = r.json()
    assert all(hit["risk"] == 0 for hit in body["hits"])
    assert body["meta"]["filtered_risk"] >= 1, "R9 的卡都是 risk 2，应该被过滤"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({}, "invalid_query"),
        ({"root_causes": ["R4"], "risk_max": 5}, "invalid_risk"),
        ({"root_causes": ["R4"], "mode": "turbo"}, "invalid_mode"),
    ],
)
def test_bad_request(payload, error):
    r = call("POST", "/api/playbook/search", payload)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == error
