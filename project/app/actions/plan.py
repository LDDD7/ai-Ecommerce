"""动作草案生成器（spec §9）：把「诊断卡 + 策略卡动作」翻译成可落库的动作参数。

这一层只负责「翻译与取数」，不做风险分级，也不改状态：

- 风险由 `store.create_action()` 内部调用 `schema.classify_risk()` 得出，
  `plan.py` 只把结果原样透传（`risk_level`），避免两处各算一套；
- 参数全部来自库里的真实数值（`store_state` / `dim_sku` / `fact_ads` /
  `inventory_snapshot` / `image_feature`），取不到就 skip，绝不硬造；
- 每张诊断卡最多 `MAX_ACTIONS_PER_CARD` 条，整批最多 `max_actions` 条；
- 同一天、同一 SKU、同一参数的重复调用由 `store.create_action()` 的幂等键去重，
  返回的动作仍会带上 `duplicate=True`，只是不计入 `meta.created`。

关于日期（确定性说明）：

- `day` 只用于幂等键，不参与任何金额/参数计算；缺省时依次回退到 `as_of`、
  `date.today()`。所以「同样的卡 + 同样的库」在同一天内一定得到同样的草案；
  跨天再跑会生成新草案（这是幂等键的设计意图：今天补过货，明天仍可再提醒）。
- `as_of` 同时作为广告/库存数据的截止日（只读 `dt <= as_of` 的记录），
  避免用未来数据给历史卡片配动作。
- 返回值里的 `meta.elapsed_ms` 是墙钟耗时，天然会变；其余字段都是确定性的。

`store_id` 也是「当前状态」的一部分：`store_state` 按 `(store_id, sku_id, field)`
隔离，找不到该店铺的记录时才回退到 `dim_sku.price_tag` / `fact_ads.cpc`。
"""

from __future__ import annotations

import math
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from app.actions.schema import SPECS, ActionError, check_guardrails
from app.actions.store import create_action, resolve_store_id

MAX_ACTIONS = 6
MAX_ACTIONS_PER_CARD = 2

#: 非 SKU 级诊断卡的固定跳过原因（前端/测试可以直接认这一句）。
REASON_NOT_SKU = "非 SKU 级诊断，动作无法定位到具体商品"

_NO_SKU = "诊断卡 scope 缺少 sku_id，动作无法定位到具体商品"
_NO_SKU_ROW = "商品不在 dim_sku 中，无法读取真实价格/成本"
_NO_ACTIONS = "策略卡没有可执行动作"
_BAD_CARD = "诊断卡格式非法（不是 dict）"


class _Skip(Exception):
    """内部信号：数据不足/越界，跳过这一条动作并记录原因。"""


# ---------------------------------------------------------------- 基础工具


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _as_float(value: Any) -> float | None:
    """宽松转 float；None/空串/NaN/Inf 一律返回 None（当作「取不到」）。"""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _as_day(value: Any) -> str | None:
    """把 date/datetime/ISO 字符串统一成 YYYY-MM-DD；无法识别时返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


def _resolve_day(day: Any, as_of: Any) -> str:
    """幂等键用的日期：显式 day > as_of > 今天（docstring 已说明）。"""
    return _as_day(day) or _as_day(as_of) or date.today().isoformat()


def _scope_of(card: Mapping[str, Any]) -> tuple[str, str | None]:
    scope = card.get("scope")
    if not isinstance(scope, Mapping):
        return "", None
    dim = str(scope.get("dim") or "").strip()
    value = scope.get("dim_value")
    sku_id = str(value).strip() if value is not None else ""
    return dim, (sku_id or None)


def _sku_row(conn: sqlite3.Connection, sku_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT sku_id, title, price_tag, cost, supplier FROM dim_sku WHERE sku_id=?",
        (sku_id,),
    ).fetchone()


def _state_value(conn: sqlite3.Connection, store_id: str, sku_id: str, field: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM store_state WHERE store_id=? AND sku_id=? AND field=?",
        (store_id, sku_id, field),
    ).fetchone()
    return None if row is None else _as_float(row["value"])


def _price_cost(
    conn: sqlite3.Connection, store_id: str, sku_id: str, sku: sqlite3.Row
) -> tuple[float | None, float | None, float | None]:
    """返回 (当前售价, 标价, 成本)；当前售价优先 store_state.price，其次 price_tag。"""
    price = _state_value(conn, store_id, sku_id, "price")
    tag = _as_float(sku["price_tag"])
    cost = _as_float(sku["cost"])
    if price is None or price <= 0:
        price = tag if tag is not None and tag > 0 else None
    return price, tag, cost


def _latest_cpc(conn: sqlite3.Connection, sku_id: str, as_of: str | None) -> float | None:
    """最近一次有曝光的广告 CPC = spend / clicks。数据表没有 cpc 列，只能现算。"""
    sql = "SELECT spend, clicks FROM fact_ads WHERE sku_id=? AND clicks > 0"
    args: list[Any] = [sku_id]
    if as_of:
        sql += " AND dt <= ?"
        args.append(as_of)
    sql += " ORDER BY dt DESC, channel_id ASC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    if row is None:
        return None
    clicks = _as_float(row["clicks"])
    spend = _as_float(row["spend"])
    if clicks is None or clicks <= 0 or spend is None:
        return None
    cpc = spend / clicks
    return cpc if cpc > 0 else None


def _latest_inventory(conn: sqlite3.Connection, sku_id: str, as_of: str | None) -> sqlite3.Row | None:
    # 真实表列名是 stock_qty（不是任务描述里的 stock），这里只认库里的真列。
    sql = "SELECT dt, stock_qty FROM inventory_snapshot WHERE sku_id=?"
    args: list[Any] = [sku_id]
    if as_of:
        sql += " AND dt <= ?"
        args.append(as_of)
    sql += " ORDER BY dt DESC LIMIT 1"
    return conn.execute(sql, args).fetchone()


def _avg_daily_sold(conn: sqlite3.Connection, sku_id: str, end_dt: str) -> float | None:
    """最近 7 天（含 end_dt）的日均销量；一天数据都没有时返回 None。

    `inventory_snapshot` 实际没有 sold 列，销量只能从 `fact_traffic_daily.paid_orders` 汇总。
    """
    try:
        end = date.fromisoformat(str(end_dt)[:10])
    except ValueError:
        return None
    start = (end - timedelta(days=6)).isoformat()
    row = conn.execute(
        "SELECT SUM(paid_orders) AS sold FROM fact_traffic_daily WHERE sku_id=? AND dt BETWEEN ? AND ?",
        (sku_id, start, end.isoformat()),
    ).fetchone()
    total = None if row is None else _as_float(row["sold"])
    if total is None:  # 没数据 ≠ 零销量：不知道就跳过，别把库存天数算成无限
        return None
    return total / 7.0


def _image_count(conn: sqlite3.Connection, sku_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM image_feature WHERE sku_id=?", (sku_id,)).fetchone()
    return int(row["n"]) if row is not None else 0


# ---------------------------------------------------------------- 策略卡解析


def _risk_of(action: Mapping[str, Any]) -> int:
    """策略卡的 risk 只用于「同卡内优先级」，最终风险仍由 store 重新分级。"""
    try:
        return int(action.get("risk", 1))
    except (TypeError, ValueError):
        return 1


def _range_of(action: Mapping[str, Any]) -> tuple[float, float] | None:
    effect = action.get("expected_effect")
    if not isinstance(effect, Mapping):
        return None
    rng = effect.get("range")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return None
    low = _as_float(rng[0])
    high = _as_float(rng[1])
    if low is None or high is None or low < 0 or high < 0 or low > high:
        return None
    return low, high


def _metric_of(action: Mapping[str, Any], action_type: str) -> str:
    effect = action.get("expected_effect")
    metric = effect.get("metric") if isinstance(effect, Mapping) else None
    text = str(metric).strip() if metric is not None else ""
    return text or SPECS[action_type].effect_metric


def _effect_payload(
    card: Mapping[str, Any],
    action: Mapping[str, Any],
    action_type: str,
    rng: tuple[float, float] | None,
) -> dict[str, Any]:
    low, high = rng if rng is not None else (0.0, 0.0)
    mid = (low + high) / 2.0
    gmv = _as_float(card.get("gmv_impact"))
    if gmv is not None and gmv != 0:
        est = round(gmv * mid, 2)
        basis = "按诊断卡影响金额 × 预期改善中值"
    else:
        est = 0.0
        basis = "缺少影响金额，仅按方向估计"
    if est == 0:
        est = 0.0  # 避免 -0.0 进 JSON
    return {
        "metric": _metric_of(action, action_type),
        "range": [low, high],
        "est_gmv_lift": est,
        "basis": basis,
    }


def _first_action_type(actions: list[Mapping[str, Any]]) -> str:
    for action in actions:
        text = str(action.get("action_type") or "").strip()
        if text:
            return text
    return ""


# ---------------------------------------------------------------- 参数生成


def _build_action(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    action: Mapping[str, Any],
    card: Mapping[str, Any],
    sku: sqlite3.Row,
    sku_id: str,
    store_id: str,
    as_of: str | None,
    title: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把一条策略卡动作翻译成 (params, effect)；数据不足时抛 `_Skip`。"""
    if action_type not in SPECS:
        raise _Skip(f"未知动作类型：{action_type or '（空）'}")
    rng = _range_of(action)
    effect = _effect_payload(card, action, action_type, rng)

    if action_type == "adjust_price":
        if rng is None:
            raise _Skip("策略卡缺少有效的 expected_effect.range，无法确定调价步长")
        current, tag, cost = _price_cost(conn, store_id, sku_id, sku)
        if current is None or current <= 0:
            raise _Skip("找不到当前售价（store_state.price / dim_sku.price_tag），无法计算调价目标价")
        step = min(0.10, max(0.03, (rng[0] + rng[1]) / 2.0))
        new_price = round(current * (1.0 - step), 2)
        if new_price <= 0:
            raise _Skip("按步长算出的目标价不大于 0")
        params: dict[str, Any] = {"new_price": new_price}
        if tag is not None and tag > 0:
            params["price_tag"] = round(tag, 2)
        check_guardrails(action_type, params, current_price=current, cost=cost)
        return params, effect

    if action_type == "adjust_bid":
        base = _state_value(conn, store_id, sku_id, "bid")
        if base is None or base <= 0:
            base = _latest_cpc(conn, sku_id, as_of)
        if base is None or base <= 0:
            raise _Skip("找不到当前出价（store_state.bid / fact_ads.cpc），无法计算目标出价")
        bid = round(base * 0.85, 3)  # R5：收缩投放，出价下调 15%
        if bid <= 0:
            raise _Skip("按 15% 降幅算出的出价不大于 0")
        params = {"bid": bid}
        check_guardrails(action_type, params, current_bid=base)
        return params, effect

    if action_type == "swap_creative":
        version = _image_count(conn, sku_id) + 1
        reason = title or str(card.get("title") or "").strip() or "更换素材"
        params = {"creative_id": f"{sku_id}-v{version}", "reason": reason}
        return params, effect

    if action_type == "issue_coupon":
        current, _tag, _cost = _price_cost(conn, store_id, sku_id, sku)
        if current is None or current <= 0:
            raise _Skip("找不到当前售价，无法计算券面额")
        amount = min(50.0, float(round(current * 0.08, 0)))
        if amount <= 0:
            raise _Skip("按售价 8% 算出的券面额为 0，不值得发券")
        quota = 100
        params = {"amount": amount, "quota": quota, "budget": round(amount * quota, 2)}
        return params, effect

    # restock_alert
    inventory = _latest_inventory(conn, sku_id, as_of)
    if inventory is None:
        raise _Skip("找不到库存快照（inventory_snapshot），无法计算可售天数")
    stock = _as_float(inventory["stock_qty"])
    if stock is None:
        raise _Skip("库存快照里的 stock_qty 不是数字")
    avg_sold = _avg_daily_sold(conn, sku_id, str(inventory["dt"]))
    if avg_sold is None:
        raise _Skip("找不到近 7 天销量（fact_traffic_daily），无法计算可售天数")
    days = round(stock / max(1.0, avg_sold), 2)
    return {"days_of_stock": days}, effect


def _plan_card(
    db_path: str | Path,
    card: Mapping[str, Any],
    actions: list[dict[str, Any]],
    *,
    store_id: str,
    trace_id: str,
    day: str,
    actor: str,
    as_of: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """单卡生成核心：返回 (已落库动作, 跳过记录)，由两个公开入口共用。"""
    card_id = str(card.get("card_id") or "")
    candidates = [a for a in actions if isinstance(a, Mapping)]
    dim, sku_id = _scope_of(card)

    if dim != "sku_id":
        return [], [{"card_id": card_id, "action": _first_action_type(candidates), "reason": REASON_NOT_SKU}]
    if not sku_id:
        return [], [{"card_id": card_id, "action": _first_action_type(candidates), "reason": _NO_SKU}]
    if not candidates:
        return [], [{"card_id": card_id, "action": "", "reason": _NO_ACTIONS}]
    if limit <= 0:
        return [], []

    out: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    conn = _connect(db_path)
    try:
        sku = _sku_row(conn, sku_id)
        if sku is None:
            return [], [{"card_id": card_id, "action": _first_action_type(candidates), "reason": _NO_SKU_ROW}]

        # 同卡内先按策略卡标注的 risk 升序，再按策略卡给定顺序稳定排序。
        ordered = sorted(enumerate(candidates), key=lambda pair: (_risk_of(pair[1]), pair[0]))
        for _index, action in ordered:
            action_type = str(action.get("action_type") or "").strip()
            title = str(action.get("title") or "").strip()
            try:
                params, effect = _build_action(
                    conn,
                    action_type=action_type,
                    action=action,
                    card=card,
                    sku=sku,
                    sku_id=sku_id,
                    store_id=store_id,
                    as_of=as_of,
                    title=title,
                )
                created = create_action(
                    db_path,
                    type=action_type,
                    params=params,
                    sku_id=sku_id,
                    store_id=store_id,
                    trace_id=trace_id,
                    reason=title,
                    actor=actor,
                    day=day,
                    effect=effect,
                )
            except _Skip as exc:
                skipped.append({"card_id": card_id, "action": action_type, "reason": str(exc)})
                continue
            except ActionError as exc:  # 护栏/参数校验失败：原因直接给异常文本
                skipped.append({"card_id": card_id, "action": action_type, "reason": str(exc)})
                continue
            out.append(created)
            if len(out) >= min(limit, MAX_ACTIONS_PER_CARD):
                break
    finally:
        conn.close()
    return out, skipped


# ---------------------------------------------------------------- 公开入口


def draft_for_card(
    db_path: str | Path,
    card: dict,
    actions: list[dict],
    *,
    store_id: str | None = None,
    trace_id: str = "",
    as_of: Any = None,
    day: Any = None,
    actor: str = "agent",
) -> list[dict]:
    """给一张诊断卡 + 它的策略卡动作，生成并落库 0~2 条动作草案。

    - 只处理 `scope.dim == "sku_id"` 的卡；其它维度返回空列表；
    - 每条动作都调用 `store.create_action()`，返回值原样带回（含 `duplicate` /
      `risk_level`），因此重复调用同一天同一参数会得到 `duplicate=True` 的既有动作；
    - 参数/护栏不通过就丢弃该条动作，不抛异常（批量入口需要继续处理下一张卡）。

    `day` 缺省时依次用 `as_of`、`date.today()` 作为幂等键日期。
    """
    if not isinstance(card, Mapping):
        return []
    store_id = resolve_store_id(db_path, store_id)
    resolved_day = _resolve_day(day, as_of)
    as_of_day = _as_day(as_of)
    card_actions = [a for a in (actions or []) if isinstance(a, Mapping)]
    out, _skipped = _plan_card(
        db_path,
        card,
        card_actions,
        store_id=store_id,
        trace_id=trace_id,
        day=resolved_day,
        actor=actor,
        as_of=as_of_day,
        limit=MAX_ACTIONS_PER_CARD,
    )
    return out


def draft_actions(
    db_path: str | Path,
    cards: list[dict],
    playbook: dict | None,
    *,
    store_id: str | None = None,
    trace_id: str = "",
    as_of: Any = None,
    day: Any = None,
    actor: str = "agent",
    max_actions: int = MAX_ACTIONS,
) -> dict:
    """批量生成动作草案。

    返回 `{"actions": [...], "meta": {created, duplicate, skipped, cards, elapsed_ms}}`：

    - `actions`：已落库（或幂等命中）的动作，每条都带 `duplicate` 与 `risk_level`；
    - `created` / `duplicate`：按 `duplicate` 字段分别计数；
    - `skipped`：`[{card_id, action, reason}]`，只记录「尝试生成但失败」的动作；
      非 SKU 卡、缺数据、护栏拒绝都在这里，原因给到可直接展示的一句话；
    - `cards`：本次处理的诊断卡数量（含被跳过的卡，即 `len(cards)`）；
    - `elapsed_ms`：墙钟耗时，仅用于观测，不参与确定性契约。

    上限：每张卡最多 `MAX_ACTIONS_PER_CARD` 条，整批最多 `max_actions` 条；
    `max_actions <= 0` 时不落库、直接返回空 `actions`。

    `playbook` 的形状与编排层 `playbook.search` 工具输出一致，按诊断卡
    `card_id` 与 `items[].card_id` 对齐；`playbook=None` 时所有卡都会被跳过。

    `day` 缺省时依次用 `as_of`、`date.today()` 作为幂等键日期（见模块 docstring）。
    """
    start = time.perf_counter()
    store_id = resolve_store_id(db_path, store_id)
    resolved_day = _resolve_day(day, as_of)
    as_of_day = _as_day(as_of)
    card_list = list(cards or [])
    cap = int(max_actions)

    result: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    created = 0
    duplicate = 0

    if cap <= 0:
        return {
            "actions": [],
            "meta": {
                "created": 0,
                "duplicate": 0,
                "skipped": [],
                "cards": len(card_list),
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            },
        }

    by_card: dict[str, Mapping[str, Any]] = {}
    if isinstance(playbook, Mapping):
        for item in playbook.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            cid = str(item.get("card_id") or "")
            if cid and cid not in by_card:
                by_card[cid] = item

    for card in card_list:
        if len(result) >= cap:
            break
        if not isinstance(card, Mapping):
            skipped.append({"card_id": "", "action": "", "reason": _BAD_CARD})
            continue
        card_id = str(card.get("card_id") or "")
        item = by_card.get(card_id)
        raw_actions = item.get("actions") if isinstance(item, Mapping) else None
        candidates = [a for a in (raw_actions or []) if isinstance(a, Mapping)]
        remaining = cap - len(result)
        card_out, card_skips = _plan_card(
            db_path,
            card,
            candidates,
            store_id=store_id,
            trace_id=trace_id,
            day=resolved_day,
            actor=actor,
            as_of=as_of_day,
            limit=min(MAX_ACTIONS_PER_CARD, remaining),
        )
        skipped.extend(card_skips)
        for action in card_out:
            result.append(action)
            if action.get("duplicate"):
                duplicate += 1
            else:
                created += 1

    return {
        "actions": result,
        "meta": {
            "created": created,
            "duplicate": duplicate,
            "skipped": skipped,
            "cards": len(card_list),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
        },
    }
