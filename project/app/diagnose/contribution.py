"""维度贡献度：把总变化拆到具体维度值上。

加和类指标（成交额、订单数、花费）用最朴素的
`ΔM = Σ_i (M_i¹ − M_i⁰)`，分量之和与总变化严格相等，残差恒为 0。

比率类指标（转化率、点击率、加购率）不能直接相减，改用**加权两期分解**：
设 R = N / D（分子/分母各自可加），则

    R₁ − R₀ = Σ_i [ (n_i¹ − R₀·d_i¹)/D₁ − (n_i⁰ − R₀·d_i⁰)/D₀ ]

每一项是「该维度自己的分子/分母相对基期水平 R₀ 的偏离」，全部加起来
正好等于 R₁ − R₀（无残差）。含义是：谁把整体转化率往下拽、拽了多少个点。

对于没有分子/分母定义的比率指标（退款率、差评率等），退化为各维度
环比差值近似：能看出「哪个维度恶化」，但分量之和不一定等于总变化，
结果里 `method="approx"` 且残差会显式报出来，不做假账。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.insights.overview import _query

# 比率指标的分子 / 分母（都必须是 catalog 里已注册的指标）
RATIO_PARTS: dict[str, tuple[str, str]] = {
    "cvr": ("paid_orders", "detail_uv"),
    "ctr": ("clicks", "impressions"),
    "add_cart_rate": ("add_cart_uv", "detail_uv"),
}

EPS = 1e-12


@dataclass
class Part:
    dim_value: str
    base: float | None
    cur: float | None
    delta: float | None          # 该维度对总变化的贡献
    share: float | None          # 占总变化的比例
    share_abs: float | None      # 占「绝对变化」的比例，判断重要性用

    def as_dict(self) -> dict[str, Any]:
        return {
            "dim_value": self.dim_value,
            "base": _r(self.base, 6),
            "cur": _r(self.cur, 6),
            "delta": _r(self.delta, 6),
            "share": _r(self.share, 4),
            "share_abs": _r(self.share_abs, 4),
        }


@dataclass
class ContributionResult:
    metric: str
    dimension: str
    method: str                       # additive | ratio | approx
    total_base: float | None
    total_cur: float | None
    delta: float | None
    delta_pct: float | None
    parts: list[Part] = field(default_factory=list)
    residual: float = 0.0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "dimension": self.dimension,
            "method": self.method,
            "total_base": _r(self.total_base, 6),
            "total_cur": _r(self.total_cur, 6),
            "delta": _r(self.delta, 6),
            "delta_pct": _r(self.delta_pct, 4),
            "residual": _r(self.residual, 8),
            "parts": [p.as_dict() for p in self.parts],
            "note": self.note,
        }

    def top(self, n: int = 5) -> list[Part]:
        ranked = [p for p in self.parts if p.delta is not None and p.dim_value != "其他"]
        ranked.sort(key=lambda p: -abs(p.delta or 0.0))
        return ranked[:n]


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _by_dim(
    db_path: str | Path,
    metrics: list[str],
    dimension: str,
    window: tuple[date, date],
) -> dict[str, dict[str, float | None]]:
    cols, rows = _query(db_path, metrics, *window, [dimension])
    out: dict[str, dict[str, float | None]] = {}
    for row in rows:
        rec = dict(zip(cols, row))
        key = str(rec.get(dimension))
        out[key] = {m: _as_float(rec.get(m)) for m in metrics}
    return out


def _finalize(
    parts: list[Part],
    total_delta: float | None,
    total_abs: float,
    *,
    top: int | None = None,
    rest_label: str = "其他",
) -> list[Part]:
    for p in parts:
        if p.delta is None:
            continue
        p.share = (p.delta / total_delta) if total_delta and abs(total_delta) > EPS else None
        p.share_abs = (abs(p.delta) / total_abs) if total_abs > EPS else None

    parts.sort(key=lambda p: -abs(p.delta or 0.0))
    if top is not None and len(parts) > top:
        head, tail = parts[:top], parts[top:]
        merged = Part(
            dim_value=rest_label,
            base=sum(p.base or 0.0 for p in tail),
            cur=sum(p.cur or 0.0 for p in tail),
            delta=sum(p.delta or 0.0 for p in tail),
            share=sum(p.share or 0.0 for p in tail) if total_delta else None,
            share_abs=sum(p.share_abs or 0.0 for p in tail),
        )
        parts = head + [merged]
    return parts


def additive_contribution(
    db_path: str | Path,
    metric: str,
    dimension: str,
    cur: tuple[date, date],
    base: tuple[date, date],
    *,
    top: int | None = None,
) -> ContributionResult:
    """加和类指标：各维度增量直接相加等于总增量。"""
    m_cur = _by_dim(db_path, [metric], dimension, cur)
    m_base = _by_dim(db_path, [metric], dimension, base)
    keys = sorted(set(m_cur) | set(m_base), key=str)

    parts: list[Part] = []
    total_cur = 0.0
    total_base = 0.0
    for k in keys:
        b = (m_base.get(k) or {}).get(metric) or 0.0
        c = (m_cur.get(k) or {}).get(metric) or 0.0
        total_base += b
        total_cur += c
        parts.append(Part(dim_value=k, base=b, cur=c, delta=c - b, share=None, share_abs=None))

    total_delta = total_cur - total_base
    total_abs = sum(abs(p.delta or 0.0) for p in parts)
    parts = _finalize(parts, total_delta, total_abs, top=top)

    return ContributionResult(
        metric=metric,
        dimension=dimension,
        method="additive",
        total_base=total_base,
        total_cur=total_cur,
        delta=total_delta,
        delta_pct=(total_delta / total_base) if total_base else None,
        parts=parts,
        residual=total_delta - sum(p.delta or 0.0 for p in parts),
        note="分量之和 = 总变化（残差 0）",
    )


def ratio_contribution(
    db_path: str | Path,
    metric: str,
    dimension: str,
    cur: tuple[date, date],
    base: tuple[date, date],
    *,
    top: int | None = None,
) -> ContributionResult:
    """比率类指标：加权两期分解，分量之和严格等于 R₁ − R₀。"""
    num, den = RATIO_PARTS[metric]
    cur_map = _by_dim(db_path, [num, den], dimension, cur)
    base_map = _by_dim(db_path, [num, den], dimension, base)
    keys = sorted(set(cur_map) | set(base_map), key=str)

    n1 = sum((cur_map.get(k) or {}).get(num) or 0.0 for k in cur_map)
    d1 = sum((cur_map.get(k) or {}).get(den) or 0.0 for k in cur_map)
    n0 = sum((base_map.get(k) or {}).get(num) or 0.0 for k in base_map)
    d0 = sum((base_map.get(k) or {}).get(den) or 0.0 for k in base_map)
    r0 = (n0 / d0) if d0 > EPS else None
    r1 = (n1 / d1) if d1 > EPS else None

    if r0 is None or r1 is None or d0 <= EPS or d1 <= EPS:
        return ContributionResult(
            metric=metric,
            dimension=dimension,
            method="ratio",
            total_base=r0,
            total_cur=r1,
            delta=None,
            delta_pct=None,
            parts=[],
            note="分母为 0，无法分解",
        )

    parts: list[Part] = []
    for k in keys:
        c = cur_map.get(k) or {}
        b = base_map.get(k) or {}
        ci = (c.get(num) or 0.0) - r0 * (c.get(den) or 0.0)
        bi = (b.get(num) or 0.0) - r0 * (b.get(den) or 0.0)
        contrib = ci / d1 - bi / d0
        parts.append(
            Part(
                dim_value=k,
                base=((b.get(num) or 0.0) / (b.get(den) or 0.0)) if (b.get(den) or 0) > EPS else None,
                cur=((c.get(num) or 0.0) / (c.get(den) or 0.0)) if (c.get(den) or 0) > EPS else None,
                delta=contrib,
                share=None,
                share_abs=None,
            )
        )

    total_delta = r1 - r0
    total_abs = sum(abs(p.delta or 0.0) for p in parts)
    parts = _finalize(parts, total_delta, total_abs, top=top)

    return ContributionResult(
        metric=metric,
        dimension=dimension,
        method="ratio",
        total_base=r0,
        total_cur=r1,
        delta=total_delta,
        delta_pct=(total_delta / r0) if r0 > EPS else None,
        parts=parts,
        residual=total_delta - sum(p.delta or 0.0 for p in parts),
        note=f"加权分解（分子 {num} / 分母 {den}），分量之和 = R₁−R₀",
    )


def approx_contribution(
    db_path: str | Path,
    metric: str,
    dimension: str,
    cur: tuple[date, date],
    base: tuple[date, date],
    *,
    top: int | None = None,
) -> ContributionResult:
    """没有分子/分母的比率指标：各维度环比差值，仅作方向参考。"""
    m_cur = _by_dim(db_path, [metric], dimension, cur)
    m_base = _by_dim(db_path, [metric], dimension, base)
    keys = sorted(set(m_cur) | set(m_base), key=str)

    parts: list[Part] = []
    for k in keys:
        b = (m_base.get(k) or {}).get(metric)
        c = (m_cur.get(k) or {}).get(metric)
        if b is None and c is None:
            continue
        parts.append(
            Part(dim_value=k, base=b, cur=c, delta=(c or 0.0) - (b or 0.0), share=None, share_abs=None)
        )

    total_cur = sum(p.cur or 0.0 for p in parts)
    total_base = sum(p.base or 0.0 for p in parts)
    total_delta = total_cur - total_base
    total_abs = sum(abs(p.delta or 0.0) for p in parts)
    parts = _finalize(parts, total_delta, total_abs, top=top)

    return ContributionResult(
        metric=metric,
        dimension=dimension,
        method="approx",
        total_base=total_base,
        total_cur=total_cur,
        delta=total_delta,
        delta_pct=(total_delta / total_base) if total_base else None,
        parts=parts,
        residual=total_delta - sum(p.delta or 0.0 for p in parts),
        note="该指标无分子/分母定义，按各维度环比差值近似，分量之和 ≠ 总变化",
    )


def contribution(
    db_path: str | Path,
    metric: str,
    dimension: str,
    cur: tuple[date, date],
    base: tuple[date, date],
    *,
    top: int | None = None,
) -> ContributionResult:
    """统一入口：自动选分解方式。"""
    if metric in RATIO_PARTS:
        return ratio_contribution(db_path, metric, dimension, cur, base, top=top)
    return additive_contribution(db_path, metric, dimension, cur, base, top=top)


def describe(result: ContributionResult, n: int = 3) -> str:
    """人读摘要：谁贡献最多、占多少。"""
    if not result.parts or result.delta is None:
        return f"{result.metric} 无可用数据。"

    pct = f"{(result.delta_pct or 0) * 100:+.1f}%" if result.delta_pct is not None else "—"
    head = f"{result.metric} 由 {result.total_base:.4g} 变为 {result.total_cur:.4g}（{pct}）"
    bits = []
    for p in result.top(n):
        # 总变化接近 0 时份额会飙到几百%，此时只报绝对量更诚实
        if p.share is None or abs(p.share) > 3:
            bits.append(f"{p.dim_value} {p.delta:+.4g}")
        else:
            bits.append(f"{p.dim_value} {p.delta:+.4g}（{p.share * 100:+.0f}%）")
    return head + "；主因：" + "、".join(bits) + "。"
