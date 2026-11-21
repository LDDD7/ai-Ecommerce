"""``app.eval.suites.action_suite`` 的离线测试。

全部用例都在 conftest 生成的 20 SKU 小库上跑；任何会写数据的操作都只发生在
``.tmp/test_eval_action/<uuid>/`` 的副本或套件自己的临时副本上，不碰 session 级
``db_path``（更不碰 ``data/store.db``）。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

from app.actions import ActionError, check_guardrails, validate_params
from app.eval.suites.action_suite import SUITE, cases, run

_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "test_eval_action"


@pytest.fixture
def workdir() -> Path:
    path = _TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def action_db(db_path: Path, workdir: Path) -> Path:
    """session 小库的一次性副本；测试里可以自由写，不污染其它测试。"""
    dst = workdir / "store.db"
    with closing(sqlite3.connect(str(db_path))) as source, closing(sqlite3.connect(str(dst))) as target:
        source.backup(target)
    return dst


def _count(db_path: Path, table: str) -> int:
    with closing(sqlite3.connect(str(db_path))) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _state_rows(db_path: Path, sku_ids: list[str]) -> list[tuple[str, str, str]]:
    if not sku_ids:
        return []
    marks = ",".join("?" for _ in sku_ids)
    with closing(sqlite3.connect(str(db_path))) as conn:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                f"SELECT store_id, sku_id, field FROM store_state WHERE sku_id IN ({marks}) ORDER BY store_id, sku_id, field",
                sku_ids,
            )
        ]


def test_cases_have_15_unique_ids_and_all_guardrail_types(db_path: Path) -> None:
    items = cases(db_path)

    assert len(items) == 15
    assert [c.case_id for c in items] == [f"A{i:02d}" for i in range(1, 16)]
    assert len({c.case_id for c in items}) == 15
    for case in items:
        assert case.suite == SUITE
        assert {"label", "kind", "type", "params", "sku_id", "expect"} <= set(case.payload)

    normal = [c for c in items if c.payload["kind"] == "create"]
    guardrails = [c for c in items if c.payload["kind"] == "guardrail"]
    assert len(normal) == 10
    assert len(guardrails) == 5

    assert Counter(c.payload["type"] for c in normal) == {
        "adjust_price": 2,
        "adjust_bid": 2,
        "swap_creative": 2,
        "issue_coupon": 2,
        "restock_alert": 2,
    }
    assert {c.payload["type"] for c in guardrails} == {
        "adjust_price",
        "adjust_bid",
        "issue_coupon",
        "swap_creative",
        "launch_rocket",
    }
    for case in normal:
        assert case.payload["expect"]["field"]
        assert case.payload["expect"]["next_value"]
    for case in guardrails:
        assert case.payload["expect"]["raises"] == "ActionError"
        assert case.payload["expect"]["contains"]


def test_run_limit_3_rolls_back_without_dirty_state(action_db: Path) -> None:
    selected = cases(action_db)[:3]
    sku_ids = [str(c.payload["sku_id"]) for c in selected]
    before = _state_rows(action_db, sku_ids)
    before_log = _count(action_db, "action_log")

    result = run(action_db, limit=3)

    assert result.suite == SUITE
    assert result.total == 3
    assert result.elapsed_ms > 0
    assert result.error is None
    assert all(c.ok for c in result.cases), [c.detail for c in result.failures()]
    for case_result in result.cases:
        detail = case_result.detail
        assert detail["executed"] is True
        assert detail["rolled_back"] is True
        assert detail["state_restored"] == detail["state_before"]
        assert detail["created"] is True

    # run() 只写自己的临时副本，传入的 action_db 必须原样。
    assert _state_rows(action_db, sku_ids) == before
    assert _count(action_db, "action_log") == before_log


def test_validate_params_and_guardrails_reject_with_messages() -> None:
    with pytest.raises(ActionError) as missing:
        validate_params("swap_creative", {})
    assert "缺少必填参数" in str(missing.value)
    assert str(missing.value).strip()

    with pytest.raises(ActionError) as over_coupon:
        validate_params("issue_coupon", {"amount": 500})
    assert "券面额" in str(over_coupon.value)
    assert str(over_coupon.value).strip()

    with pytest.raises(ActionError) as price_rise:
        check_guardrails("adjust_price", {"new_price": 150.0}, current_price=100.0, cost=10.0)
    assert "涨幅" in str(price_rise.value)
    assert str(price_rise.value).strip()

    with pytest.raises(ActionError) as bid_rise:
        check_guardrails("adjust_bid", {"bid": 3.0}, current_bid=1.0)
    assert "出价涨幅" in str(bid_rise.value)
    assert str(bid_rise.value).strip()


def test_run_does_not_modify_source_database(db_path: Path) -> None:
    before_log = _count(db_path, "action_log")
    before_state = _count(db_path, "store_state")
    before_audit = _count(db_path, "audit_log")

    result = run(db_path, limit=5)

    assert result.total == 5
    assert result.error is None
    assert _count(db_path, "action_log") == before_log
    assert _count(db_path, "store_state") == before_state
    assert _count(db_path, "audit_log") == before_audit


def test_full_run_all_15_pass_and_meta_counts(action_db: Path) -> None:
    result = run(action_db)

    assert result.total == 15
    assert result.error is None
    assert result.passed == 15, [(c.case_id, c.detail) for c in result.failures()]
    assert result.meta["created"] == 10
    assert result.meta["rejected"] == 5
    assert sum(result.meta["risk_counts"].values()) == 10
    assert result.meta["avg_ms"] > 0
    for case_result in result.cases:
        if case_result.detail["kind"] == "guardrail":
            assert case_result.detail["message"].strip()
            assert case_result.detail["rejected"] is True


def test_run_missing_database_returns_error(workdir: Path) -> None:
    result = run(workdir / "missing" / "store.db")

    assert result.suite == SUITE
    assert result.total == 0
    assert result.error
    assert "无法生成动作用例" in result.error
