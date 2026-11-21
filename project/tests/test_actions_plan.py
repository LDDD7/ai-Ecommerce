"""动作草案生成器（`app.actions.plan`）的离线契约测试。

约定：
- 每个用例把 session 级 `db_path` 备份到 `tmp_path`，再清空 `action_log` 与
  `store_001` 的 `store_state`，保证幂等键/当前价从干净状态开始；
- `day` 统一用 2031 年的唯一值，避免与共享库里的历史动作撞幂等键；
- 期望值尽量从复制库里用 SQL 现算，不在测试里硬编码商品价格/库存。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.actions import plan
from app.actions.plan import MAX_ACTIONS_PER_CARD, draft_actions, draft_for_card
from app.actions.schema import SPECS

AS_OF = "2026-11-18"
DAY = "2031-01-01"


# ------------------------------------------------------------------ 夹具/工具


@pytest.fixture()
def tmp_path():
    """本机 %TEMP% 子目录 ACL 异常（见 tests/conftest.py），测试产物一律落工作区 `.tmp/`。"""
    root = Path(__file__).resolve().parents[1] / ".tmp" / "pytest_plan"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def db(db_path, tmp_path):
    """小库副本：session 级 db_path 会被其它测试写 action_log，必须隔离。"""
    target = tmp_path / "plan.db"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _execute(db=target, sql="DELETE FROM action_log")
    _execute(db=target, sql="DELETE FROM store_state WHERE store_id='store_001'")
    return target


def _execute(*, db, sql, args=()):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _rows(db, sql, args=()):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _one(db, sql, args=()):
    rows = _rows(db, sql, args)
    return rows[0] if rows else None


def _clone_sku(db, *, sku_id, price, cost):
    """复制 SKU0001 的其它必填列，造一个价格/成本可控的测试 SKU。"""
    _execute(
        db=db,
        sql="""
        INSERT INTO dim_sku
            (sku_id, title, category_l1, category_l2, price_tag, cost, launch_date,
             image_version, supplier, style, fabric, color, price_band)
        SELECT ?, title, category_l1, category_l2, ?, ?, launch_date,
               image_version, supplier, style, fabric, color, price_band
          FROM dim_sku WHERE sku_id='SKU0001'
        """,
        args=(sku_id, price, cost),
    )


def _card(sku_id="SKU0001", *, card_id="card_001", code="R5", dim="sku_id", gmv_impact=-1000.0, title=None):
    return {
        "card_id": card_id,
        "code": code,
        "title": title or f"{code} 诊断卡",
        "scope": {"dim": dim, "dim_label": "商品", "dim_value": sku_id},
        "gmv_impact": gmv_impact,
        "confidence": 0.66,
        "narrative": "测试用诊断卡",
        "material": {},
    }


def _play(actions, *, card_id="card_001"):
    return {
        "items": [
            {
                "card_id": card_id,
                "code": "R5",
                "title": "测试策略卡",
                "gmv_impact": -1000.0,
                "actions": list(actions),
            }
        ],
        "meta": {"query": "test"},
    }


def _act(action_type, *, risk=1, title=None, range_=(0.03, 0.05), metric=None, card_id="PB-TEST-01"):
    return {
        "card_id": card_id,
        "title": title or f"{action_type} 动作",
        "action_type": action_type,
        "risk": risk,
        "expected_effect": {
            "metric": metric or SPECS[action_type].effect_metric,
            "direction": "up",
            "range": list(range_),
            "window_days": 7,
        },
    }


def _run(db, card, actions, *, day=DAY, max_actions=plan.MAX_ACTIONS):
    return draft_actions(db, [card], _play(actions, card_id=card["card_id"]), as_of=AS_OF, day=day, max_actions=max_actions)


# ------------------------------------------------------------------ 规则覆盖


def test_non_sku_card_is_skipped_with_reason(db):
    res = _run(db, _card("甜美", dim="style"), [_act("adjust_price")])
    assert res["actions"] == []
    assert len(res["meta"]["skipped"]) == 1
    skip = res["meta"]["skipped"][0]
    assert skip["reason"] == plan.REASON_NOT_SKU
    assert skip["action"] == "adjust_price"


def test_adjust_price_uses_price_tag_and_review_risk(db):
    tag = _one(db, "SELECT price_tag FROM dim_sku WHERE sku_id='SKU0001'")["price_tag"]
    assert _one(db, "SELECT 1 FROM store_state WHERE store_id='store_001' AND sku_id='SKU0001'") is None

    res = _run(db, _card(), [_act("adjust_price", range_=(0.03, 0.03))])
    assert res["meta"]["skipped"] == []
    assert len(res["actions"]) == 1
    action = res["actions"][0]
    assert action["type"] == "adjust_price"
    assert action["params"]["new_price"] == pytest.approx(round(tag * 0.97, 2))
    assert action["params"]["price_tag"] == pytest.approx(tag)
    assert action["risk_level"] == 1
    assert action["duplicate"] is False


def test_adjust_price_deep_cut_is_manager_risk(db):
    tag = _one(db, "SELECT price_tag FROM dim_sku WHERE sku_id='SKU0001'")["price_tag"]
    res = _run(db, _card(), [_act("adjust_price", range_=(0.09, 0.09))], day="2031-01-02")
    action = res["actions"][0]
    assert action["params"]["new_price"] == pytest.approx(round(tag * 0.91, 2))
    assert action["risk_level"] == 2  # 跌幅 9% 超过 5% 阈值


def test_adjust_price_skips_when_margin_below_floor(db):
    _clone_sku(db, sku_id="SKU_MARGIN", price=100.0, cost=99.0)
    res = _run(db, _card("SKU_MARGIN"), [_act("adjust_price")], day="2031-01-03")
    assert res["actions"] == []
    assert len(res["meta"]["skipped"]) == 1
    reason = res["meta"]["skipped"][0]["reason"]
    assert "毛利率" in reason
    assert "低于底线" in reason


def test_adjust_price_prefers_store_state_price(db):
    _execute(
        db=db,
        sql="INSERT INTO store_state (store_id, sku_id, field, value, updated_at) VALUES (?,?,?,?,?)",
        args=("store_001", "SKU0002", "price", "500", "2026-11-18T00:00:00"),
    )
    res = _run(db, _card("SKU0002"), [_act("adjust_price", range_=(0.03, 0.03))], day="2031-01-04")
    action = res["actions"][0]
    assert action["params"]["new_price"] == pytest.approx(485.0)


def test_adjust_bid_skips_without_cpc(db):
    _clone_sku(db, sku_id="SKU_NOCPC", price=200.0, cost=80.0)
    # 确认这个 SKU 没有任何广告点击数据
    assert _one(db, "SELECT 1 FROM fact_ads WHERE sku_id='SKU_NOCPC'") is None
    res = _run(db, _card("SKU_NOCPC"), [_act("adjust_bid")], day="2031-01-05")
    assert res["actions"] == []
    reason = res["meta"]["skipped"][0]["reason"]
    assert "出价" in reason


def test_adjust_bid_uses_latest_cpc(db):
    row = _one(
        db,
        """
        SELECT spend, clicks FROM fact_ads
         WHERE sku_id='SKU0001' AND clicks > 0 AND dt <= ?
         ORDER BY dt DESC, channel_id ASC LIMIT 1
        """,
        (AS_OF,),
    )
    base = row["spend"] / row["clicks"]
    res = _run(db, _card(), [_act("adjust_bid")], day="2031-01-06")
    assert res["meta"]["skipped"] == []
    action = res["actions"][0]
    assert action["params"]["bid"] == pytest.approx(round(base * 0.85, 3))
    assert action["params"]["bid"] < base
    assert action["risk_level"] == 1  # 降价不会升级风险


def test_swap_creative_increments_version(db):
    before = _one(db, "SELECT COUNT(*) AS n FROM image_feature WHERE sku_id='SKU0001'")["n"]
    first = _run(db, _card(), [_act("swap_creative")], day="2031-01-07")
    assert first["actions"][0]["params"]["creative_id"] == f"SKU0001-v{before + 1}"

    _execute(
        db=db,
        sql="""
        INSERT INTO image_feature (image_id, sku_id, role, path, features_json, source, created_at)
        VALUES ('img_test_extra', 'SKU0001', 'detail', 'data/images/test.png', '{}', 'mock', '2026-11-18T00:00:00')
        """,
    )
    after = _one(db, "SELECT COUNT(*) AS n FROM image_feature WHERE sku_id='SKU0001'")["n"]
    second = _run(db, _card(), [_act("swap_creative")], day="2031-01-08")
    assert second["actions"][0]["params"]["creative_id"] == f"SKU0001-v{after + 1}"


def test_issue_coupon_amount_budget_for_low_price(db):
    _clone_sku(db, sku_id="SKU_CHEAP", price=148.0, cost=60.0)
    res = _run(db, _card("SKU_CHEAP"), [_act("issue_coupon")], day="2031-01-09")
    action = res["actions"][0]
    assert action["params"]["amount"] == pytest.approx(12.0)  # round(148*0.08)=12
    assert action["params"]["quota"] == 100
    assert action["params"]["budget"] == pytest.approx(1200.0)
    assert action["risk_level"] == 1


def test_issue_coupon_budget_over_2000_is_manager_risk(db):
    tag = _one(db, "SELECT price_tag FROM dim_sku WHERE sku_id='SKU0001'")["price_tag"]
    amount = min(50.0, float(round(tag * 0.08, 0)))
    res = _run(db, _card(), [_act("issue_coupon")], day="2031-01-10")
    action = res["actions"][0]
    assert action["params"]["amount"] == pytest.approx(amount)
    assert action["params"]["budget"] == pytest.approx(amount * 100)
    assert action["params"]["budget"] > 2000
    assert action["risk_level"] == 2


def test_restock_alert_days_of_stock(db):
    inv = _one(
        db,
        "SELECT dt, stock_qty FROM inventory_snapshot WHERE sku_id='SKU0001' AND dt <= ? ORDER BY dt DESC LIMIT 1",
        (AS_OF,),
    )
    end = date.fromisoformat(inv["dt"])
    start = end - timedelta(days=6)
    sold = _one(
        db,
        "SELECT SUM(paid_orders) AS sold FROM fact_traffic_daily WHERE sku_id='SKU0001' AND dt BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    )["sold"]
    expected = round(inv["stock_qty"] / max(1.0, sold / 7.0), 2)

    res = _run(db, _card(), [_act("restock_alert", range_=(0.2, 0.45))], day="2031-01-11")
    action = res["actions"][0]
    assert action["params"]["days_of_stock"] == pytest.approx(expected)
    assert action["risk_level"] == 0
    assert action["effect"]["metric"] == "stockout_rate"


def test_per_card_limit_two_prefers_low_risk(db):
    actions = [
        _act("swap_creative", risk=1),
        _act("restock_alert", risk=0, range_=(0.2, 0.45)),
        _act("issue_coupon", risk=1),
    ]
    res = _run(db, _card(), actions, day="2031-01-12")
    assert len(res["actions"]) == MAX_ACTIONS_PER_CARD == 2
    # restock 的 risk 更小，先于策略卡里排在前面的 swap
    assert [a["type"] for a in res["actions"]] == ["restock_alert", "swap_creative"]


def test_global_max_actions(db):
    skus = [
        r["sku_id"]
        for r in _rows(db, "SELECT sku_id FROM dim_sku ORDER BY sku_id LIMIT 4")
    ]
    cards = [_card(sku, card_id=f"card_{sku}") for sku in skus]
    items = [
        {
            "card_id": f"card_{sku}",
            "code": "R5",
            "title": "测试策略卡",
            "gmv_impact": -1000.0,
            "actions": [_act("swap_creative")],
        }
        for sku in skus
    ]
    res = draft_actions(
        db, cards, {"items": items, "meta": {}}, as_of=AS_OF, day="2031-01-13", max_actions=3
    )
    assert len(res["actions"]) == 3
    assert res["meta"]["cards"] == 4
    assert res["meta"]["created"] == 3


def test_duplicate_calls_return_duplicate_flag(db):
    card = _card()
    actions = [_act("adjust_price", range_=(0.03, 0.03))]
    first = _run(db, card, actions, day="2031-01-14")
    assert first["meta"]["created"] == 1
    assert first["meta"]["duplicate"] == 0
    count_before = _one(db, "SELECT COUNT(*) AS n FROM action_log")["n"]

    second = _run(db, card, actions, day="2031-01-14")
    assert second["meta"]["created"] == 0
    assert second["meta"]["duplicate"] == 1
    assert second["actions"][0]["duplicate"] is True
    assert second["actions"][0]["action_id"] == first["actions"][0]["action_id"]
    assert _one(db, "SELECT COUNT(*) AS n FROM action_log")["n"] == count_before


def test_max_actions_zero_returns_empty(db):
    res = _run(db, _card(), [_act("swap_creative")], day="2031-01-15", max_actions=0)
    assert res["actions"] == []
    assert res["meta"]["created"] == 0
    assert res["meta"]["duplicate"] == 0
    assert res["meta"]["skipped"] == []
    assert res["meta"]["cards"] == 1
    assert _one(db, "SELECT COUNT(*) AS n FROM action_log")["n"] == 0


def test_result_is_json_serializable(db):
    actions = [_act("issue_coupon"), _act("restock_alert", range_=(0.2, 0.45))]
    res = _run(db, _card(), actions, day="2031-01-16")
    text = json.dumps(res, ensure_ascii=False)
    assert "NaN" not in text
    parsed = json.loads(text)
    assert parsed["actions"][0]["params"]
    assert parsed["meta"]["created"] == 2
    assert isinstance(parsed["meta"]["elapsed_ms"], (int, float))


def test_effect_estimate_from_gmv_impact(db):
    card = _card(gmv_impact=-1000.0, card_id="card_effect")
    res = _run(db, card, [_act("adjust_price", range_=(0.03, 0.05))], day="2031-01-17")
    effect = res["actions"][0]["effect"]
    assert effect["est_gmv_lift"] == pytest.approx(-40.0)  # -1000 × 0.04
    assert effect["range"] == [0.03, 0.05]
    assert effect["basis"] == "按诊断卡影响金额 × 预期改善中值"
    assert effect["metric"] == "cvr"


def test_effect_without_gmv_impact_uses_direction_only(db):
    card = _card(gmv_impact=0, card_id="card_effect_zero")
    res = _run(db, card, [_act("restock_alert", range_=(0.2, 0.45))], day="2031-01-18")
    effect = res["actions"][0]["effect"]
    assert effect["est_gmv_lift"] == 0.0
    assert effect["basis"] == "缺少影响金额，仅按方向估计"


def test_draft_for_card_returns_list_and_non_sku_is_empty(db):
    ok = draft_for_card(
        db, _card(), [_act("swap_creative")], as_of=AS_OF, day="2031-01-19"
    )
    assert isinstance(ok, list)
    assert len(ok) == 1
    assert draft_for_card(
        db, _card("甜美", dim="style"), [_act("swap_creative")], as_of=AS_OF, day="2031-01-20"
    ) == []
