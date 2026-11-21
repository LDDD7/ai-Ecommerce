"""转化漏斗：曝光 → 点击 → 详情页 → 加购 → 支付。

漏斗的价值不在于「画一个好看的漏斗」，而在于**定位是哪一级漏了**：
整体转化率 = 各级转化率的连乘，取对数后每一级的贡献可以直接相加：

    ln(整体₁/整体₀) = Σ ln(步进转化率ᵢ₁ / 步进转化率ᵢ₀)

所以「整体转化率跌了 12%，其中加购率贡献 -9 个百分点」这种话是可以算出来的，
不是拍脑袋。`worst_step()` 直接给最该动手的那一级。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from app.insights.overview import _query

STEPS: tuple[tuple[str, str], ...] = (
    ("impressions", "曝光"),
    ("clicks", "点击"),
    ("detail_uv", "详情页"),
    ("add_cart_uv", "加购"),
    ("paid_orders", "支付"),
)

STEP_LABELS = dict(STEPS)


@dataclass
class Step:
    name: str
    display: str
    base: float
    cur: float
    rate_base: float | None       # 相对上一级的转化率
    rate_cur: float | None
    rate_rel: float | None        # 转化率相对变化
    log_contrib: float | None
    share: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display": self.display,
            "base": round(self.base, 6),
            "cur": round(self.cur, 6),
            "rate_base": _r(self.rate_base, 8),
            "rate_cur": _r(self.rate_cur, 8),
            "rate_rel": _r(self.rate_rel, 6),
            "log_contrib": _r(self.log_contrib, 6),
            "share": _r(self.share, 4),
        }


@dataclass
class Funnel:
    scope: str
    steps: list[Step] = field(default_factory=list)
    overall_base: float | None = None
    overall_cur: float | None = None
    delta: float | None = None
    delta_pct: float | None = None
    log_total: float | None = None
    residual: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "steps": [s.as_dict() for s in self.steps],
            "overall_base": _r(self.overall_base, 8),
            "overall_cur": _r(self.overall_cur, 8),
            "delta": _r(self.delta, 8),
            "delta_pct": _r(self.delta_pct, 6),
            "log_total": _r(self.log_total, 6),
            "residual": _r(self.residual, 10),
        }

    def worst_step(self) -> Step | None:
        """对整体下滑"责任最大"的一级：log 贡献最负；整体没恶化时返回 None。"""
        cands = [s for s in self.steps if s.log_contrib is not None and s.log_contrib < 0]
        if not cands:
            return None
        return min(cands, key=lambda s: s.log_contrib or 0.0)

    def best_step(self) -> Step | None:
        """把整体往上拉得最多的一级。"""
        cands = [s for s in self.steps if s.log_contrib is not None and s.log_contrib > 0]
        if not cands:
            return None
        return max(cands, key=lambda s: s.log_contrib or 0.0)


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def _safe_log_ratio(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or cur <= 0 or base <= 0:
        return None
    return math.log(cur / base)


def _fill(values: dict[tuple[str, str], float], scope: str) -> Funnel:
    """给定两个窗口的各级绝对量，算出步进转化率与 log 贡献。"""
    steps: list[Step] = []
    prev_b: float | None = None
    prev_c: float | None = None
    for name, display in STEPS:
        b = float(values.get(("base", name), 0.0) or 0.0)
        c = float(values.get(("cur", name), 0.0) or 0.0)
        rate_b = (b / prev_b) if prev_b else None
        rate_c = (c / prev_c) if prev_c else None
        rel = ((rate_c - rate_b) / rate_b) if (rate_b and rate_c is not None) else None
        steps.append(
            Step(
                name=name,
                display=display,
                base=b,
                cur=c,
                rate_base=rate_b,
                rate_cur=rate_c,
                rate_rel=rel,
                log_contrib=_safe_log_ratio(rate_c, rate_b),
                share=None,
            )
        )
        prev_b, prev_c = b, c

    overall_b = steps[-1].base / steps[0].base if steps[0].base else None
    overall_c = steps[-1].cur / steps[0].cur if steps[0].cur else None
    log_total = _safe_log_ratio(overall_c, overall_b)

    for s in steps:
        if s.log_contrib is not None and log_total:
            s.share = s.log_contrib / log_total

    delta = (overall_c - overall_b) if (overall_b is not None and overall_c is not None) else None
    return Funnel(
        scope=scope,
        steps=steps,
        overall_base=overall_b,
        overall_cur=overall_c,
        delta=delta,
        delta_pct=(delta / overall_b) if (delta is not None and overall_b) else None,
        log_total=log_total,
        residual=(log_total - sum(s.log_contrib or 0.0 for s in steps)) if log_total is not None else None,
    )


def funnel(
    db_path: str | Path,
    cur: tuple[date, date],
    base: tuple[date, date],
    dimension: str | None = None,
    scope: str = "全店",
) -> Funnel:
    """全店（或单维度值）的转化漏斗两期对比。"""
    metrics = [name for name, _ in STEPS]
    cols_c, rows_c = _query(db_path, metrics, *cur, [dimension] if dimension else None)
    cols_b, rows_b = _query(db_path, metrics, *base, [dimension] if dimension else None)
    mc = dict(zip(cols_c, rows_c[0] if rows_c else []))
    mb = dict(zip(cols_b, rows_b[0] if rows_b else []))

    values: dict[tuple[str, str], float] = {}
    for name in metrics:
        values[("cur", name)] = float(mc.get(name) or 0.0)
        values[("base", name)] = float(mb.get(name) or 0.0)
    return _fill(values, scope)


def funnel_by_dim(
    db_path: str | Path,
    cur: tuple[date, date],
    base: tuple[date, date],
    dimension: str,
    *,
    limit: int = 8,
) -> list[Funnel]:
    """按维度拆开看漏斗，只保留基期曝光最大的若干维度值，避免长尾噪声。"""
    metrics = [name for name, _ in STEPS]
    cols_c, rows_c = _query(db_path, metrics, *cur, [dimension], limit=500)
    cols_b, rows_b = _query(db_path, metrics, *base, [dimension], limit=500)
    base_map = {str(dict(zip(cols_b, r))[dimension]): dict(zip(cols_b, r)) for r in rows_b}

    funnels: list[Funnel] = []
    for row in rows_c:
        rec = dict(zip(cols_c, row))
        key = str(rec[dimension])
        prev = base_map.get(key, {})
        values: dict[tuple[str, str], float] = {}
        for name in metrics:
            values[("cur", name)] = float(rec.get(name) or 0.0)
            values[("base", name)] = float(prev.get(name) or 0.0)
        funnels.append(_fill(values, key))

    funnels.sort(key=lambda f: -(f.steps[0].base if f.steps else 0.0))
    return funnels[:limit]


def describe(f: Funnel) -> str:
    """人读摘要：整体怎么变、哪一级最该背锅。"""
    if f.overall_base is None or f.overall_cur is None:
        return f"{f.scope}：数据不足，无法比较漏斗。"

    pct = f"{(f.delta_pct or 0) * 100:+.1f}%"
    head = (
        f"{f.scope}：曝光→支付 {f.overall_base * 100:.3f}% → {f.overall_cur * 100:.3f}%"
        f"（{pct}）。"
    )
    worst = f.worst_step()
    best = f.best_step()
    bits = []
    if worst is not None and worst.rate_rel is not None:
        bits.append(f"最差一级：{worst.display}（转化率 {worst.rate_rel * 100:+.1f}%）")
    if best is not None and best.rate_rel is not None and (worst is None or best is not worst):
        bits.append(f"最好一级：{best.display}（{best.rate_rel * 100:+.1f}%）")
    if not bits:
        return head + "各级转化率基本持平。"
    return head + "；".join(bits) + "。"
