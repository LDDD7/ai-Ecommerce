"""第二轮 replan 的接线测试：build_diagnosis / HTTP / SSE 三处都要真的用上它。

这一层不重复测 replan 的判定逻辑（`tests/test_replan.py` 已覆盖），只钉：
1. `_run_replan` 会按动作改卡片（升级抬置信度、降权乘系数、撤销移出列表）；
2. 复检出错时**不吞诊断**（degraded + 卡片原样返回）；
3. `with_replan=False` 与 HTTP `?replan=false` 能关掉它（消融要用）；
4. SSE 会发 `critic` 事件，前端才可能显示「已复审 N 张」。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.diagnose import diagnosis as diag_mod
from app.diagnose.diagnosis import _run_replan, build_diagnosis
from app.main import create_app
from app.orchestrator.events import EVENT_TYPES
from app.orchestrator.runner import run_chat


# ---------------------------------------------------------------- 构造工具


def card(card_id: str, code: str, *, dim: str = "sku_id", value: str = "SKU0001", confidence: float = 0.5) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "code": code,
        "title": f"{code} 测试卡",
        "scope": {"dim": dim, "dim_label": "商品", "dim_value": value},
        "confidence": confidence,
        "evidence": [{"metric": "cvr", "z": 4.2, "delta_pct": -0.3}],
        "caveats": [],
    }


def verdict(card_id: str, code: str, state: str = "weaken") -> dict[str, Any]:
    return {
        "card_id": card_id,
        "code": code,
        "verdict": state,
        "checks": [{"name": "upstream", "ok": False}],
    }


class FakeRecheck:
    """`ScopeRecheck` 的最小替身：只要 `as_dict()` 与契约字段。"""

    def __init__(self, scope: tuple[str, str], *, found: bool, required_found: bool, metric: str) -> None:
        self.scope = scope
        self.found = found
        self.required_found = required_found
        self.metric = metric

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": list(self.scope),
            "days": diag_mod.REPLAN_DAYS,
            "as_of": "2026-11-17",
            "scope_found": self.found,
            "required_metric": self.metric,
            "required_found": self.required_found,
            "metrics": [self.metric] if self.required_found else [],
            "points": [],
            "elapsed_ms": 0.1,
        }


def install_recheck(monkeypatch, table: dict[tuple[str, str], FakeRecheck]) -> list[list[tuple[str, str]]]:
    """把 `recheck_many` 换掉，并记录被要求复检的 scope。"""
    calls: list[list[tuple[str, str]]] = []

    def fake(db_path, scopes, *, as_of=None, required=None, days=14, config=None, cache=None):  # noqa: ANN001
        calls.append(list(scopes))
        return {scope: table[scope] for scope in scopes if scope in table}

    monkeypatch.setattr(diag_mod, "recheck_many", fake)
    return calls


# ---------------------------------------------------------------- 动作落地


def test_upgrade_raises_confidence_and_marks_support(monkeypatch):
    cards = [card("card_001", "R5")]
    verdicts = [verdict("card_001", "R5")]
    install_recheck(monkeypatch, {("sku_id", "SKU0001"): FakeRecheck(("sku_id", "SKU0001"), found=True, required_found=True, metric="roi")})

    dropped: list[dict[str, Any]] = []
    kept, meta = _run_replan("data/store.db", cards, verdicts, "2026-11-17", dropped)

    assert meta["upgrade"] == 1 and meta["drop"] == 0 and meta["degraded"] is False
    assert len(kept) == 1
    assert kept[0]["confidence"] >= diag_mod.REPLAN_UPGRADE_CONF
    assert kept[0]["critic"]["verdict"] == "support"
    assert kept[0]["replan"]["action"] == "upgrade"
    assert any("补到 roi" in line for line in kept[0]["caveats"])
    assert dropped == []


def test_drop_removes_card_and_records_reason(monkeypatch):
    cards = [card("card_002", "R4")]
    verdicts = [verdict("card_002", "R4")]
    install_recheck(monkeypatch, {("sku_id", "SKU0001"): FakeRecheck(("sku_id", "SKU0001"), found=False, required_found=False, metric="return_rate")})

    dropped: list[dict[str, Any]] = []
    kept, meta = _run_replan("data/store.db", cards, verdicts, "2026-11-17", dropped)

    assert meta["drop"] == 1
    assert kept == []
    assert dropped and dropped[0]["reason"].startswith("第二轮复审撤销：")
    assert "return_rate" in dropped[0]["reason"]


def test_demote_multiplies_confidence_without_dropping(monkeypatch):
    cards = [card("card_003", "R7", confidence=0.6)]
    verdicts = [verdict("card_003", "R7")]
    calls = install_recheck(monkeypatch, {})

    dropped: list[dict[str, Any]] = []
    kept, meta = _run_replan("data/store.db", cards, verdicts, "2026-11-17", dropped)

    assert meta["demote"] == 1 and meta["drop"] == 0
    assert len(kept) == 1
    assert kept[0]["confidence"] == pytest.approx(0.6 * diag_mod.REPLAN_DEMOTE_FACTOR, abs=1e-6)
    # R7 没有必需辅证 → 不该浪费一次复检
    assert calls == []


def test_recheck_failure_degrades_but_keeps_cards(monkeypatch):
    cards = [card("card_004", "R5")]
    verdicts = [verdict("card_004", "R5")]

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr(diag_mod, "recheck_many", boom)

    dropped: list[dict[str, Any]] = []
    kept, meta = _run_replan("data/store.db", cards, verdicts, "2026-11-17", dropped)

    assert meta["degraded"] is True and "db locked" in meta["reason"]
    assert kept == cards and dropped == []


def test_support_cards_are_not_rechecked(monkeypatch):
    cards = [card("card_005", "R5")]
    verdicts = [verdict("card_005", "R5", state="support")]
    calls = install_recheck(monkeypatch, {})

    kept, meta = _run_replan("data/store.db", cards, verdicts, "2026-11-17", [])

    assert kept == cards
    assert meta["checked"] == 0 and calls == []


# ---------------------------------------------------------------- 开关与接线


def test_build_diagnosis_can_disable_replan(db_path):
    body = build_diagnosis(db_path, with_replan=False)
    replan = body["meta"]["critic"]["replan"]
    assert replan["enabled"] is False
    assert replan["checked"] == 0


def test_build_diagnosis_reports_replan_on_small_db(db_path):
    body = build_diagnosis(db_path)
    replan = body["meta"]["critic"]["replan"]
    assert replan["enabled"] is True
    assert replan["days"] == diag_mod.REPLAN_DAYS
    assert replan["elapsed_ms"] >= 0
    # 统计口径必须自洽：四类动作之和 == 处理过的卡数
    assert replan["upgrade"] + replan["drop"] + replan["demote"] + replan["skip"] == replan["checked"]
    for item in replan["items"]:
        assert item["action"] in {"upgrade", "drop", "demote", "skip"}


def call(method: str, path: str, json: Any = None):
    async def _run():
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.request(method, path, json=json)

    return asyncio.run(_run())


def test_api_diagnose_exposes_replan_and_can_toggle():
    on = call("GET", "/api/diagnose?max_cards=2")
    assert on.status_code == 200
    assert on.json()["meta"]["critic"]["replan"]["enabled"] is True

    off = call("GET", "/api/diagnose?max_cards=2&replan=false")
    assert off.status_code == 200
    assert off.json()["meta"]["critic"]["replan"]["enabled"] is False


def test_critic_event_is_whitelisted_and_emitted(db_path):
    assert "critic" in EVENT_TYPES

    async def collect():
        return [ev async for ev in run_chat("11-17 那周成交额为什么掉了", as_of="2026-11-18", db_path=db_path)]

    events = asyncio.run(collect())
    critic = [e for e in events if e.name == "critic"]
    assert critic, "诊断之后必须发 critic 事件，否则前端看不到复审结论"
    data = critic[0].data
    assert data["checked"] >= 0
    assert data["replan"]["enabled"] is True
    assert {"upgrade", "drop", "demote", "skip"} <= set(data["replan"])
