"""动作 Schema：五类动作的参数、风险分级与幂等键（spec §9）。

这个文件是「契约」——`plan.py`（生成草案）、`store.py`（落库与状态机）、
`execute.py`（mock 执行与回滚）都只认这里的定义，避免三处各写一套参数名。

设计要点：
- **风险不写在 YAML 里靠人记，而是算出来的**：同一类动作，改价 3% 是 risk 1，
  改价 12% 就是 risk 2。`classify_risk()` 是唯一入口。
- **幂等键包含日期**：同一天对同一个 SKU 做同样的动作只算一次；跨天允许再来一次
  （今天没补货，明天当然可以再提醒）。
- **参数校验宁可严**：执行层是 mock，但参数错了会把 `store_state` 写脏，
  回滚也救不回来。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

ACTION_TYPES: tuple[str, ...] = (
    "adjust_price",
    "adjust_bid",
    "swap_creative",
    "issue_coupon",
    "restock_alert",
)

RISK_AUTO = 0  # 自动执行（只做提醒）
RISK_REVIEW = 1  # 单人审批
RISK_MANAGER = 2  # 店长审批（改价 >5%、加投 >20%）

RISK_LABELS = {RISK_AUTO: "低风险", RISK_REVIEW: "需人审", RISK_MANAGER: "高风险·必须人审"}
STATUSES = ("drafted", "approved", "rejected", "executed", "rolled_back")

# 状态机：只允许这些迁移，别的一律拒绝（防止前端乱调接口把状态写坏）
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "drafted": ("approved", "rejected", "executed"),  # risk 0 允许跳过审批直接执行
    "approved": ("executed", "rejected"),
    "rejected": (),
    "executed": ("rolled_back",),
    "rolled_back": (),
}


@dataclass(frozen=True)
class ActionSpec:
    type: str
    label: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    effect_metric: str
    store_field: str
    description: str
    risk_note: str = ""


SPECS: dict[str, ActionSpec] = {
    "adjust_price": ActionSpec(
        type="adjust_price",
        label="调价",
        required=("new_price",),
        optional=("price_tag",),
        effect_metric="cvr",
        store_field="price",
        description="把 SKU 售价调到 new_price；>5% 的变动需要店长审批",
        risk_note="降幅 >5% 或涨幅 >10% → risk 2",
    ),
    "adjust_bid": ActionSpec(
        type="adjust_bid",
        label="调出价",
        required=("bid",),
        optional=("plan_id", "budget_delta"),
        effect_metric="roi",
        store_field="bid",
        description="调整投放出价；加投 >20% 需要店长审批",
        risk_note="bid 涨幅 >20% 或加投 >¥200 → risk 2",
    ),
    "swap_creative": ActionSpec(
        type="swap_creative",
        label="换素材",
        required=("creative_id",),
        optional=("image_path", "reason"),
        effect_metric="ctr",
        store_field="creative",
        description="替换主图/素材；只换图不动价，所以默认单人审批",
        risk_note="固定 risk 1",
    ),
    "issue_coupon": ActionSpec(
        type="issue_coupon",
        label="发券",
        required=("amount",),
        optional=("threshold", "quota", "budget"),
        effect_metric="paid_orders",
        store_field="coupon",
        description="发一张满减券；总预算 >2000 元需要店长审批",
        risk_note="单张 >50 元或总额 >2000 元 → risk 2",
    ),
    "restock_alert": ActionSpec(
        type="restock_alert",
        label="补货预警",
        required=(),
        optional=("days_of_stock", "supplier"),
        effect_metric="stockout_rate",
        store_field="stock",
        description="给供应链发补货提醒，不改前台状态，所以可以自动执行",
        risk_note="固定 risk 0",
    ),
}

# 安全边界：mock 执行也要有护栏，否则一次误判就能把 300 个 SKU 全改价
MAX_PRICE_DROP = 0.30  # 降价幅度上限（相对当前价）
MAX_PRICE_RISE = 0.30
MAX_BID_RISE = 0.50
MAX_COUPON_AMOUNT = 100.0
MAX_COUPON_BUDGET = 5_000.0
MAX_BUDGET_DELTA = 1_000.0  # 单次加投金额上限（元）
MAX_BUDGET_DELTA_REVIEW = 200.0  # 超过这个金额就要店长审
MIN_MARGIN = 0.05  # 调价后毛利率不得低于 5%


class ActionError(ValueError):
    """动作参数/状态非法。路由层统一转 400。"""


def spec_of(action_type: str) -> ActionSpec:
    try:
        return SPECS[action_type]
    except KeyError as exc:
        raise ActionError(f"未知动作类型：{action_type}（可选：{', '.join(ACTION_TYPES)}）") from exc


def _as_float(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ActionError(f"{name} 必须是数字") from exc
    if out != out or out in (float("inf"), float("-inf")):
        raise ActionError(f"{name} 不能是 NaN/Inf")
    return out


def validate_params(action_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化参数；返回新的 dict，不改入参。"""
    spec = spec_of(action_type)
    params = dict(params or {})
    missing = [k for k in spec.required if params.get(k) in (None, "")]
    if missing:
        raise ActionError(f"{spec.label}缺少必填参数：{', '.join(missing)}")

    allowed = set(spec.required) | set(spec.optional)
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ActionError(f"{spec.label}不支持这些参数：{', '.join(unknown)}")

    clean: dict[str, Any] = {}
    if action_type == "adjust_price":
        price = _as_float(params["new_price"], "new_price")
        if price <= 0:
            raise ActionError("new_price 必须大于 0")
        clean["new_price"] = round(price, 2)
        if params.get("price_tag") is not None:
            clean["price_tag"] = round(_as_float(params["price_tag"], "price_tag"), 2)
    elif action_type == "adjust_bid":
        bid = _as_float(params["bid"], "bid")
        if bid <= 0:
            raise ActionError("bid 必须大于 0")
        clean["bid"] = round(bid, 3)
        if params.get("budget_delta") is not None:
            clean["budget_delta"] = round(_as_float(params["budget_delta"], "budget_delta"), 2)
        if params.get("plan_id") is not None:
            clean["plan_id"] = str(params["plan_id"])[:64]
    elif action_type == "swap_creative":
        cid = str(params["creative_id"]).strip()
        if not cid:
            raise ActionError("creative_id 不能为空")
        clean["creative_id"] = cid[:64]
        for key in ("image_path", "reason"):
            if params.get(key) is not None:
                clean[key] = str(params[key])[:300]
    elif action_type == "issue_coupon":
        amount = _as_float(params["amount"], "amount")
        if amount <= 0:
            raise ActionError("券面额必须大于 0")
        if amount > MAX_COUPON_AMOUNT:
            raise ActionError(f"券面额不能超过 {MAX_COUPON_AMOUNT:.0f} 元")
        clean["amount"] = round(amount, 2)
        if params.get("threshold") is not None:
            clean["threshold"] = round(_as_float(params["threshold"], "threshold"), 2)
        if params.get("quota") is not None:
            clean["quota"] = int(_as_float(params["quota"], "quota"))
        budget = params.get("budget")
        if budget is None:
            budget = clean["amount"] * clean.get("quota", 100)
        budget = _as_float(budget, "budget")
        if budget > MAX_COUPON_BUDGET:
            raise ActionError(f"券总预算不能超过 {MAX_COUPON_BUDGET:.0f} 元")
        clean["budget"] = round(budget, 2)
    elif action_type == "restock_alert":
        if params.get("days_of_stock") is not None:
            clean["days_of_stock"] = round(_as_float(params["days_of_stock"], "days_of_stock"), 2)
        if params.get("supplier") is not None:
            clean["supplier"] = str(params["supplier"])[:64]
    return clean


def classify_risk(
    action_type: str,
    params: dict[str, Any],
    *,
    current_price: float | None = None,
    current_bid: float | None = None,
) -> int:
    """按「动作 + 幅度」算风险等级，而不是按动作类型一刀切。"""
    spec_of(action_type)  # 校验类型存在

    if action_type == "restock_alert":
        return RISK_AUTO
    if action_type == "swap_creative":
        return RISK_REVIEW

    if action_type == "adjust_price":
        price = float(params.get("new_price") or 0)
        base = float(current_price or params.get("price_tag") or 0)
        if base <= 0:
            return RISK_MANAGER  # 不知道原价就不要自动改价
        change = (price - base) / base
        if change <= -0.05 or change >= 0.10:
            return RISK_MANAGER
        return RISK_REVIEW

    if action_type == "adjust_bid":
        bid = float(params.get("bid") or 0)
        base = float(current_bid or 0)
        if base > 0 and (bid - base) / base > 0.20:
            return RISK_MANAGER
        # budget_delta 是「加投多少钱」，不是比例（比例用 budget_delta_pct 会另开字段）
        delta = float(params.get("budget_delta") or 0)
        if delta > MAX_BUDGET_DELTA_REVIEW:
            return RISK_MANAGER
        return RISK_REVIEW

    if action_type == "issue_coupon":
        amount = float(params.get("amount") or 0)
        quota = params.get("quota")
        # 只有显式给了 budget 或 quota 才估总额；两个都没给说明还没定发多少张，
        # 不能凭空按 100 张算出一个 5000 元预算把风险抬到 2。
        budget = float(params.get("budget") or 0)
        if budget <= 0 and quota:
            budget = amount * float(quota)
        if amount > 50 or budget > 2_000:
            return RISK_MANAGER
        return RISK_REVIEW

    return RISK_REVIEW


def idempotency_key(
    action_type: str,
    store_id: str,
    sku_id: str | None,
    params: dict[str, Any],
    *,
    day: str | date | None = None,
) -> str:
    """`sha256(type + store_id + sku_id + date + params_hash)`（spec §9）。"""
    if day is None:
        day_s = date.today().isoformat()
    elif isinstance(day, date):
        day_s = day.isoformat()
    else:
        day_s = str(day)
    payload = json.dumps(params or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = "|".join([action_type, store_id or "", sku_id or "", day_s, payload])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_guardrails(
    action_type: str,
    params: dict[str, Any],
    *,
    current_price: float | None = None,
    cost: float | None = None,
    current_bid: float | None = None,
) -> list[str]:
    """执行前的硬护栏；返回警告列表（空 = 通过），越界直接抛 ActionError。"""
    spec_of(action_type)
    warnings: list[str] = []

    if action_type == "adjust_price":
        price = float(params["new_price"])
        base = float(current_price or 0)
        if base > 0:
            change = (price - base) / base
            if change < -MAX_PRICE_DROP:
                raise ActionError(f"降幅 {abs(change):.0%} 超过上限 {MAX_PRICE_DROP:.0%}，请分批执行")
            if change > MAX_PRICE_RISE:
                raise ActionError(f"涨幅 {change:.0%} 超过上限 {MAX_PRICE_RISE:.0%}")
            if abs(change) >= 0.05:
                warnings.append(f"价格变动 {change:+.1%}，属于价格带调整，需留意转化与竞品")
        if cost is not None and cost > 0:
            margin = (price - cost) / price
            if margin < MIN_MARGIN:
                raise ActionError(f"调价后毛利率 {margin:.1%} 低于底线 {MIN_MARGIN:.0%}")

    if action_type == "adjust_bid":
        bid = float(params["bid"])
        if bid <= 0:
            raise ActionError("出价必须大于 0")
        base = float(current_bid or 0)
        if base > 0 and (bid - base) / base > MAX_BID_RISE:
            raise ActionError(f"出价涨幅超过上限 {MAX_BID_RISE:.0%}")
        delta = float(params.get("budget_delta") or 0)
        if delta > MAX_BUDGET_DELTA:
            raise ActionError(f"单次加投 ¥{delta:.0f} 超过上限 ¥{MAX_BUDGET_DELTA:.0f}，请分批调")

    return warnings


def action_summary(action_type: str, params: dict[str, Any], sku_id: str | None = None) -> str:
    """一句人话，用于前端列表与报告。"""
    spec = spec_of(action_type)
    who = sku_id or "全店"
    if action_type == "adjust_price":
        return f"{who} 调价至 ¥{params['new_price']:.2f}"
    if action_type == "adjust_bid":
        return f"{who} 出价调至 ¥{params['bid']:.3f}"
    if action_type == "swap_creative":
        return f"{who} 换素材 {params['creative_id']}"
    if action_type == "issue_coupon":
        return f"{who} 发 ¥{params['amount']:.0f} 券（预算 ¥{params.get('budget', 0):.0f}）"
    if action_type == "restock_alert":
        return f"{who} 补货预警"
    return f"{who} {spec.label}"
