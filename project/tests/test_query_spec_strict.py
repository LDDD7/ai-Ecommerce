"""QuerySpec 严格校验：多写一个字段必须报错，不能「假成功」。

背景：pydantic 默认丢弃未知字段，`dims`（正确是 `dimensions`）会被静默忽略，
返回 200 但结果退化成全量汇总——调用方以为口径对了，其实错了。
这里把「未知字段」逐层钉死：顶层、time、filters、order_by。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.metrics.spec import QuerySpec


def call(method: str, path: str, json: Any = None):
    async def _run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=json)

    return asyncio.run(_run())


GOOD_SPEC = {
    "metrics": ["gmv"],
    "dimensions": ["dt"],
    "time": {"start": "2026-11-11", "end": "2026-11-17"},
    "limit": 20,
}


# ---------------------------------------------------------------------------
# 模型层
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_rejected():
    with pytest.raises(ValidationError) as ei:
        QuerySpec(metrics=["gmv"], dims=["dt"])  # type: ignore[call-arg]
    errors = ei.value.errors()
    assert errors[0]["type"] == "extra_forbidden"
    assert errors[0]["loc"][-1] == "dims"


def test_unknown_nested_field_rejected():
    with pytest.raises(ValidationError) as ei:
        QuerySpec(metrics=["gmv"], time={"start": "2026-11-01", "end": "2026-11-07", "tz": "UTC"})
    assert ei.value.errors()[0]["loc"][-1] == "tz"


def test_unknown_filter_field_rejected():
    with pytest.raises(ValidationError):
        QuerySpec(
            metrics=["gmv"],
            filters=[{"field": "style", "op": "=", "value": "甜美", "note": "x"}],
        )


def test_unknown_order_by_field_rejected():
    with pytest.raises(ValidationError):
        QuerySpec(metrics=["gmv"], order_by=[{"metric": "gmv", "desc": True}])


def test_window_days_is_not_a_spec_field():
    """窗口必须用 time 区间表达；window_days 是解析层的参数，不是契约字段。"""
    with pytest.raises(ValidationError):
        QuerySpec(metrics=["gmv"], window_days=7)  # type: ignore[call-arg]


def test_good_spec_still_valid():
    spec = QuerySpec.model_validate(GOOD_SPEC)
    assert spec.dimensions == ["dt"]
    assert spec.limit == 20


# ---------------------------------------------------------------------------
# HTTP 层：400 invalid_spec，并且报错里点名未知字段
# ---------------------------------------------------------------------------


def test_http_unknown_field_returns_invalid_spec():
    bad = dict(GOOD_SPEC)
    bad["dims"] = ["dt"]
    r = call("POST", "/api/query", json={"spec": bad})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid_spec"
    detail = body["detail"]
    assert any("dims" in e["loc"] for e in detail), detail
    hit = next(e for e in detail if e["loc"].endswith("dims"))
    assert "未知字段" in hit["msg"]
    assert "dimensions" in hit["msg"], "提示里应列出可用字段"


def test_http_nested_unknown_field_returns_invalid_spec():
    bad = dict(GOOD_SPEC)
    bad["time"] = {"start": "2026-11-11", "end": "2026-11-17", "granularity": "day"}
    r = call("POST", "/api/query", json={"spec": bad})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid_spec"
    assert any(e["loc"].endswith("granularity") for e in body["detail"])


def test_http_good_spec_still_runs():
    r = call("POST", "/api/query", json={"spec": GOOD_SPEC})
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 7
    assert body["spec"]["dimensions"] == ["dt"]


def test_http_missing_spec_still_reports_loc():
    r = call("POST", "/api/query", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_spec"


# ---------------------------------------------------------------------------
# 回归：自然语言解析产出的 spec 必须能直接过严格校验
# ---------------------------------------------------------------------------


def test_nl_result_spec_passes_strict_validation():
    from app.query.nl import parse_question

    for question in ("近 7 天甜美款的成交额", "上月各渠道支付订单数", "11 月以来按天的转化率"):
        parsed = parse_question(question, as_of="2026-11-17", use_llm=False)
        spec = QuerySpec.model_validate(parsed.spec)  # 多一个字段这里就会炸
        assert spec.time is not None
        assert "window_days" not in parsed.spec


def test_nl_http_still_ok():
    r = call("POST", "/api/query/nl", json={"question": "近 7 天成交额按天", "as_of": "2026-11-17", "use_llm": False})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] is not None, body.get("warnings")
