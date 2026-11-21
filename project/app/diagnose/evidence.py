"""证据链：一切结论都必须挂在证据上。

依据 spec §7.5：`Evidence{metric, dim_field, dim_value, window, baseline, current,
delta, contribution_pct}`，**缺证据不允许出结论**——`is_complete()` 不通过时，
上层必须把结论降级为「数据不足」，而不是编一个原因。

证据的两个层次：
- 直接证据：异常检测给出的「指标 × 维度值」双窗口对比（本模块转换）；
- 归因证据：该维度值对总变化贡献了多少钱/多少个百分点（`contribution_pct`，
  由 `diagnosis.py` 用维度贡献度回填，避免在这里重复查库）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

REQUIRED_FIELDS = ("metric", "dim_field", "dim_value", "window", "baseline", "current", "delta")


@dataclass
class Evidence:
    metric: str
    display: str
    dim_field: str
    dim_value: str
    window: dict[str, str]
    baseline: float | None
    current: float | None
    delta: float | None
    delta_pct: float | None = None
    z: float | None = None
    contribution_pct: float | None = None
    unit: str | None = None
    source: str = "zscore-v1"
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "display": self.display,
            "dim_field": self.dim_field,
            "dim_value": self.dim_value,
            "window": self.window,
            "baseline": _r(self.baseline, 6),
            "current": _r(self.current, 6),
            "delta": _r(self.delta, 6),
            "delta_pct": _r(self.delta_pct, 6),
            "z": _r(self.z, 2),
            "contribution_pct": _r(self.contribution_pct, 6),
            "unit": self.unit,
            "source": self.source,
            "note": self.note,
        }

    @property
    def key(self) -> str:
        return f"{self.metric}@{self.dim_field}={self.dim_value}"

    @classmethod
    def from_point(cls, point: Any, *, contribution_pct: float | None = None) -> "Evidence":
        """从 `AnomalyPoint` 转换。用 duck typing，避免循环 import。"""
        win = getattr(point, "window", None)
        window = win.as_dict() if hasattr(win, "as_dict") else (win or {})
        return cls(
            metric=point.metric,
            display=getattr(point, "display", point.metric),
            dim_field=getattr(point, "dimension", ""),
            dim_value=str(getattr(point, "dim_value", "")),
            window=window,
            baseline=getattr(point, "baseline", None),
            current=getattr(point, "current", None),
            delta=getattr(point, "delta", None),
            delta_pct=getattr(point, "delta_pct", None),
            z=getattr(point, "z", None),
            contribution_pct=contribution_pct,
            unit=getattr(point, "unit", None),
        )


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def is_complete(ev: Evidence) -> bool:
    """证据是否够格支撑一条结论。"""
    for name in REQUIRED_FIELDS:
        if getattr(ev, name, None) in (None, ""):
            return False
    if not ev.window.get("start") or not ev.window.get("end"):
        return False
    return True


def complete_only(items: Iterable[Evidence]) -> list[Evidence]:
    return [e for e in items if is_complete(e)]


MONEY_UNITS = ("元", "currency")
RATIO_UNITS = ("ratio",)


def fmt_num(value: Any, unit: str | None = None) -> str:
    """按单位格式化数字：金额千分位、比率转百分比、其余按量级取舍。

    `:.4g` 会把 5.8 万打成 `5.841e+04`，在中文诊断文案里非常刺眼，
    所以统一走这里。
    """
    if value is None:
        return "—"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit in RATIO_UNITS:
        return f"{x * 100:.2f}%"
    if unit in MONEY_UNITS:
        return f"{x:,.0f}"
    ax = abs(x)
    if ax >= 10000:
        return f"{x:,.0f}"
    if ax >= 100:
        return f"{x:.1f}"
    if ax >= 1:
        return f"{x:.2f}"
    return f"{x:.4f}"


def summarize(items: Iterable[Evidence], limit: int = 5) -> str:
    """把一组证据压成一行中文，给卡片摘要或 trace 用。"""
    picked = list(items)[:limit]
    if not picked:
        return "无可引用证据"
    bits = []
    for e in picked:
        pct = f"{e.delta_pct * 100:+.1f}%" if e.delta_pct is not None else "—"
        bits.append(f"{e.display}({e.dim_field}={e.dim_value}) {pct}")
    return "；".join(bits)
