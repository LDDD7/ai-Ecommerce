"""自然语言查数（A+B）的单元与接口测试。

规则解析是确定性的，直接断言；LLM 解析用注入的 FakeLLM，绝不联网。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.query.nl import parse_question


class FakeLLM:
    """只实现 chat_json 的假网关。"""

    def __init__(self, payload: Any = None, exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.calls = 0
        self.last_messages: list[dict[str, Any]] | None = None

    def chat_json(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        self.last_messages = messages
        if self.exc:
            raise self.exc
        if isinstance(self.payload, str):
            return json.loads(self.payload)
        return self.payload


def call(method: str, path: str, json_body: Any = None):
    async def _run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=json_body)

    return asyncio.run(_run())


# ---------- 规则解析 ----------


def test_rule_window_and_daily_grain():
    r = parse_question("近 14 天成交额和转化率按天看", as_of="2026-11-17", window_days=7, use_llm=False)
    spec = r.spec
    assert r.source == "rule"
    assert spec["metrics"] == ["gmv", "cvr"]
    assert spec["dimensions"] == ["dt"]
    assert r.window_days == 14
    assert spec["time"] == {"start": "2026-11-04", "end": "2026-11-17"}


def test_rule_style_filter():
    r = parse_question("甜美款近 7 天退款率", as_of="2026-11-17", use_llm=False)
    assert r.spec["metrics"] == ["refund_rate"]
    assert r.spec["filters"] == [{"field": "style", "op": "=", "value": "甜美"}]


def test_rule_channel_dimension():
    r = parse_question("按渠道看投放 ROI", as_of="2026-11-17", use_llm=False)
    assert "channel_id" in r.spec["dimensions"]
    assert r.spec["metrics"] == ["roi"]


def test_rule_defaults_when_nothing_recognised():
    r = parse_question("这周怎么样", as_of="2026-11-17", use_llm=False)
    assert r.spec["metrics"] == ["gmv", "paid_orders"]
    assert r.window_days == 7
    assert r.warnings and "成交额" in r.warnings[0]


def test_rule_month_word():
    r = parse_question("最近一个月各款式客单价", as_of="2026-11-17", use_llm=False)
    assert r.window_days == 30
    assert r.spec["dimensions"] == ["style"]


def test_empty_question_raises():
    try:
        parse_question("   ", use_llm=False)
    except ValueError as e:
        assert "不能为空" in str(e)
    else:  # pragma: no cover
        raise AssertionError("空问题应当报错")


# ---------- LLM 解析 ----------


def test_llm_success_marks_source():
    fake = FakeLLM({"metrics": ["ctr", "clicks"], "dimensions": ["channel_id"], "window_days": 14, "filters": [], "limit": 500})
    r = parse_question("各渠道点击率", as_of="2026-11-17", window_days=7, llm=fake)
    assert r.source == "llm"
    assert r.spec["metrics"] == ["ctr", "clicks"]
    assert r.spec["dimensions"] == ["channel_id"]
    assert r.window_days == 14
    assert r.spec["time"]["start"] == "2026-11-04"
    assert fake.calls == 1
    # 提示词里必须有指标与「只输出 JSON」
    system = fake.last_messages[0]["content"]
    assert "gmv" in system and "只输出" in system


def test_llm_unknown_metric_is_filtered():
    fake = FakeLLM({"metrics": ["gmv", "drop_table"], "dimensions": ["dt"], "window_days": 7})
    r = parse_question("成交额", as_of="2026-11-17", llm=fake)
    assert r.spec["metrics"] == ["gmv"]


def test_llm_only_unknown_metric_falls_back_to_defaults():
    fake = FakeLLM({"metrics": ["drop_table"], "dimensions": [], "window_days": 7})
    r = parse_question("成交额", as_of="2026-11-17", llm=fake)
    assert r.spec["metrics"] == ["gmv", "paid_orders"]
    assert any("回退" in w for w in r.warnings)


def test_llm_dimension_cap():
    fake = FakeLLM(
        {
            "metrics": ["gmv"],
            "dimensions": ["dt", "style", "channel_id", "sku_id", "color"],
            "window_days": 7,
        }
    )
    r = parse_question("成交额", as_of="2026-11-17", llm=fake)
    assert len(r.spec["dimensions"]) <= 3


def test_llm_window_clamped():
    fake = FakeLLM({"metrics": ["gmv"], "dimensions": [], "window_days": 9999})
    r = parse_question("成交额", as_of="2026-11-17", llm=fake)
    assert r.window_days <= 90


def test_llm_failure_falls_back_to_rule():
    fake = FakeLLM(exc=RuntimeError("boom"))
    r = parse_question("近 7 天甜美款转化率", as_of="2026-11-17", llm=fake)
    assert r.source == "rule"
    assert r.spec["metrics"] == ["cvr"]
    assert any("回退规则解析" in w for w in r.warnings)


def test_llm_invalid_dimension_for_metric_falls_back_safely():
    """维度不被指标支持时，_validate 抛错 → 退到最小可跑查询，而不是 500。"""
    fake = FakeLLM({"metrics": ["roi"], "dimensions": ["style"], "window_days": 7})
    r = parse_question("款式 ROI", as_of="2026-11-17", llm=fake)
    # roi 只支持 channel/sku 等维度，style 不合法 → 至少保证 spec 是合法的
    assert r.spec["metrics"]
    assert isinstance(r.spec["time"], dict)


# ---------- HTTP 接口 ----------


def test_api_query_nl_success():
    r = call(
        "POST",
        "/api/query/nl",
        {"question": "近 7 天成交额按天看", "window_days": 7, "use_llm": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "rule"
    assert body["spec"]["metrics"] == ["gmv"]
    assert body["result"] is not None
    assert body["result"]["columns"][0] == "dt"
    assert body["result"]["meta"]["row_count"] >= 1
    assert body["as_of"]


def test_api_query_nl_empty_question_400():
    r = call("POST", "/api/query/nl", {"question": "", "use_llm": False})
    assert r.status_code == 400


def test_api_query_nl_bad_window_400():
    r = call("POST", "/api/query/nl", {"question": "成交额", "window_days": 0, "use_llm": False})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_window"


def test_api_query_nl_second_call_is_cached():
    """同一句话 + 同一基准日，第二次解析应命中进程内 TTL 缓存（避免重复等 4s 的模型解析）。"""
    payload = {"question": "缓存用例：近 7 天成交额按天看", "window_days": 7, "use_llm": False}
    first = call("POST", "/api/query/nl", payload).json()
    second = call("POST", "/api/query/nl", payload).json()
    assert first["meta"]["cached"] is False
    assert second["meta"]["cached"] is True
    assert first["spec"] == second["spec"]
