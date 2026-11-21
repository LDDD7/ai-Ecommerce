"""GMV 恒等式分解：GMV = UV × CVR × AOV。

两种口径互补，都要给：

- **对数分解**：ln(GMV₁/GMV₀) = Σ ln(fᵢ₁/fᵢ₀)。各项天然可加，用来回答
  「这一波下滑里，转化率占了几个百分点」，不受量级影响。
- **链式增量分解**：ΔGMV = ΔU·C₀·A₀ + U₁·ΔC·A₀ + U₁·C₁·ΔA。望远镜求和，
  残差恒为 0，用来回答「少做了多少钱」，可以按因子直接相加核对。

注意 UV 用的是详情页 UV（`uv` 指标），所以 CVR 的分母也是详情页 UV，
`uv × cvr × aov` 与订单视图的 GMV 是同一个口径；两者的差来自比率四舍五入，
`identity_gap_pct` 会把它报出来（正常应 < 0.1%）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from app.insights.overview import _query

FACTOR_DISPLAY = {"uv": "流量", "cvr": "转化率", "aov": "客单价"}
GMV_FACTORS: tuple[str, ...] = ("uv", "cvr", "aov")


@dataclass
class Factor:
    name: str
    display: str
    base: float | None
    cur: float | None
    rel: float | None = None            # 相对变化 (cur-base)/base
    log_contrib: float | None = None    # ln(cur/base)
    log_share: float | None = None      # log_contrib / log_total
    money_contrib: float | None = None  # 链式分解下该因子贡献的金额
    money_share: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display": self.display,
            "base": _round(self.base, 6),
            "cur": _round(self.cur, 6),
            "rel": _round(self.rel, 6),
            "log_contrib": _round(self.log_contrib, 6),
            "log_share": _round(self.log_share, 4),
            "money_contrib": _round(self.money_contrib, 2),
            "money_share": _round(self.money_share, 4),
        }


@dataclass
class Decomposition:
    factors: list[Factor]
    total_base: float | None
    total_cur: float | None
    delta: float | None
    delta_pct: float | None
    log_total: float | None
    residual_money: float
    identity_gap_pct: float | None
    factor_product_base: float | None = None
    factor_product_cur: float | None = None
    method: str = "log+chain"
    subject: str = "成交额"

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "subject": self.subject,
            "factors": [f.as_dict() for f in self.factors],
            "total_base": _round(self.total_base, 2),
            "total_cur": _round(self.total_cur, 2),
            "delta": _round(self.delta, 2),
            "delta_pct": _round(self.delta_pct, 6),
            "log_total": _round(self.log_total, 6),
            "residual_money": _round(self.residual_money, 2),
            "identity_gap_pct": _round(self.identity_gap_pct, 6),
            "factor_product_base": _round(self.factor_product_base, 2),
            "factor_product_cur": _round(self.factor_product_cur, 2),
        }

    def top(self, n: int = 3) -> list[Factor]:
        """按对总数的 log 贡献绝对值排序，返回最关键的 n 个因子。"""
        ranked = [f for f in self.factors if f.log_contrib is not None]
        ranked.sort(key=lambda f: -abs(f.log_contrib or 0.0))
        return ranked[:n]


def _round(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def _safe_log_ratio(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or cur <= 0 or base <= 0:
        return None
    return math.log(cur / base)


def decompose_product(
    values_base: dict[str, float | None],
    values_cur: dict[str, float | None],
    factors: Sequence[str],
    *,
    total_base: float | None = None,
    total_cur: float | None = None,
    display: dict[str, str] | None = None,
    subject: str = "成交额",
) -> Decomposition:
    """对 total = Π factors 做对数分解 + 链式增量分解。

    `total_base/total_cur` 可显式传入真实总量（例如直接从订单视图取的 GMV），
    传 None 时用因子连乘，此时 `identity_gap_pct` 为 0。
    """
    labels = {**FACTOR_DISPLAY, **(display or {})}
    prod_b = 1.0
    prod_c = 1.0
    for name in factors:
        b = values_base.get(name)
        c = values_cur.get(name)
        prod_b *= float(b) if b is not None else 0.0
        prod_c *= float(c) if c is not None else 0.0

    t_base = float(total_base) if total_base is not None else prod_b
    t_cur = float(total_cur) if total_cur is not None else prod_c
    delta = t_cur - t_base
    delta_pct = (delta / t_base) if t_base else None

    log_total = _safe_log_ratio(t_cur, t_base)

    items: list[Factor] = []
    running_cur = 1.0
    running_money = 0.0
    for idx, name in enumerate(factors):
        b = values_base.get(name)
        c = values_cur.get(name)
        bf = float(b) if b is not None else 0.0
        cf = float(c) if c is not None else 0.0

        # 链式增量：Δfᵢ × (前面因子用当期值) × (后面因子用基期值)，望远镜求和残差为 0
        rest_base = 1.0
        for other in factors[idx + 1 :]:
            ob = values_base.get(other)
            rest_base *= float(ob) if ob is not None else 0.0
        money = (cf - bf) * running_cur * rest_base
        running_cur *= cf
        running_money += money

        items.append(
            Factor(
                name=name,
                display=labels.get(name, name),
                base=bf,
                cur=cf,
                rel=((cf - bf) / bf) if bf else None,
                log_contrib=_safe_log_ratio(cf, bf),
                money_contrib=money,
            )
        )

    for item in items:
        if log_total and item.log_contrib is not None:
            item.log_share = item.log_contrib / log_total
        if delta:
            item.money_share = (item.money_contrib or 0.0) / delta

    gap = None
    if t_cur and total_cur is not None:
        gap = (prod_c - t_cur) / t_cur

    return Decomposition(
        factors=items,
        total_base=t_base,
        total_cur=t_cur,
        delta=delta,
        delta_pct=delta_pct,
        log_total=log_total,
        residual_money=delta - running_money,
        identity_gap_pct=gap,
        factor_product_base=prod_b,
        factor_product_cur=prod_c,
        subject=subject,
    )


def gmv_decompose(
    db_path: str | Path,
    cur: tuple[date, date],
    base: tuple[date, date],
) -> Decomposition:
    """从库里取 UV/CVR/AOV/GMV 两个窗口的值，做一次完整分解。"""
    cols_c, rows_c = _query(db_path, ["uv", "cvr", "aov", "gmv"], *cur)
    cols_b, rows_b = _query(db_path, ["uv", "cvr", "aov", "gmv"], *base)
    mc = dict(zip(cols_c, rows_c[0] if rows_c else []))
    mb = dict(zip(cols_b, rows_b[0] if rows_b else []))

    values_cur = {"uv": mc.get("uv"), "cvr": mc.get("cvr"), "aov": mc.get("aov")}
    values_base = {"uv": mb.get("uv"), "cvr": mb.get("cvr"), "aov": mb.get("aov")}
    return decompose_product(
        values_base,
        values_cur,
        GMV_FACTORS,
        total_base=mb.get("gmv"),
        total_cur=mc.get("gmv"),
    )


def decompose_orders(
    db_path: str | Path,
    cur: tuple[date, date],
    base: tuple[date, date],
) -> Decomposition:
    """订单数分解：orders = UV × CVR（同一口径，少一个 AOV 因子）。"""
    cols_c, rows_c = _query(db_path, ["uv", "cvr", "paid_orders"], *cur)
    cols_b, rows_b = _query(db_path, ["uv", "cvr", "paid_orders"], *base)
    mc = dict(zip(cols_c, rows_c[0] if rows_c else []))
    mb = dict(zip(cols_b, rows_b[0] if rows_b else []))
    return decompose_product(
        {"uv": mb.get("uv"), "cvr": mb.get("cvr")},
        {"uv": mc.get("uv"), "cvr": mc.get("cvr")},
        ("uv", "cvr"),
        total_base=mb.get("paid_orders"),
        total_cur=mc.get("paid_orders"),
        display={"uv": "流量", "cvr": "转化率"},
        subject="支付订单数",
    )


def describe(dec: Decomposition) -> str:
    """人读摘要：结论先行，再按贡献排序给因子。"""
    if dec.delta is None or dec.total_cur is None or dec.total_base is None:
        return "两个窗口缺数据，无法分解。"

    head = (
        f"{dec.subject} {dec.total_base:,.0f} → {dec.total_cur:,.0f}"
        f"（{dec.delta:+,.0f}，{(dec.delta_pct or 0) * 100:+.1f}%）。"
    )
    parts = []
    for f in dec.top(3):
        if f.log_contrib is None:
            continue
        rel = f"{(f.rel or 0) * 100:+.1f}%" if f.rel is not None else "—"
        money = f"{f.money_contrib:+,.0f}" if f.money_contrib is not None else "—"
        parts.append(f"{f.display} {rel}（{money}）")
    body = "；".join(parts) if parts else "各因子无可用数据"
    return f"{head}{body}。"
