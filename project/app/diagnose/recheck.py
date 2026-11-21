"""同 scope 复检：在更宽的窗口里找同一张卡的证据（R1 契约）。

定位：只读数据层。它只回答三个问题，不做判定：
1. 这个 ``(维度, 值)`` 在更宽窗口里还异常吗？（``scope_found``）
2. 它的异常指标集合里，有没有该根因的必需辅证？（``required_found``）
3. 那些指标各自的当前值 / 基线 / 变化 / z 是多少？（``points``）

判定交给 :mod:`app.diagnose.replan`，本模块不 import 它，避免循环依赖。

性能约束：一次 ``detect`` 实测 0.65–1.0s（650 格），同一 ``(db_path, as_of, days)``
必须复用结果——用调用方传入的 ``cache`` 字典；不传就不缓存（测试友好）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.diagnose.anomaly import AnomalyPoint, DetectConfig, DetectionResult, detect

#: 默认复检窗口：7 天诊断窗 → 14 天复检窗。
DEFAULT_RECHECK_DAYS = 14

#: 命中缓存时记录的占位耗时（毫秒）：只表示「没有重新计算」，不是真实测量值。
_CACHE_HIT_MS = 0.05

#: ``points`` 最多保留的条数（卡片上够用，再多也没人看）。
_MAX_POINTS = 8


@dataclass(slots=True)
class ScopeRecheck:
    """单个 scope 的复检结论。"""

    scope: tuple[str, str]
    days: int
    as_of: str
    scope_found: bool
    required_metric: str | None
    required_found: bool
    metrics: list[str] = field(default_factory=list)
    points: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """给 :func:`app.diagnose.replan.plan_recovery` 用的扁平结构。"""
        return {
            "scope": list(self.scope),
            "days": self.days,
            "as_of": self.as_of,
            "scope_found": self.scope_found,
            "required_metric": self.required_metric,
            "required_found": self.required_found,
            "metrics": list(self.metrics),
            "points": list(self.points),
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


# ---------------------------------------------------------------- 私有辅助


def _normalize_as_of(as_of: str | date) -> str:
    """把 ``as_of`` 统一成 ``YYYY-MM-DD``（``date`` 与 ISO 字符串都接受）。"""
    if isinstance(as_of, datetime):  # datetime 是 date 的子类，先判更具体的
        return as_of.date().isoformat()
    if isinstance(as_of, date):
        return as_of.isoformat()
    if isinstance(as_of, str):
        return date.fromisoformat(as_of.strip()).isoformat()
    raise TypeError(f"as_of 需要 str 或 datetime.date，收到 {type(as_of).__name__}")


def _detect_cached(
    db_path: str,
    as_of_iso: str,
    days: int,
    config: Any,
    cache: dict[Any, Any] | None,
) -> tuple[DetectionResult, float]:
    """取一次巡检结果（必要时才真算），返回 ``(结果, 耗时毫秒)``。

    缓存键为 ``(resolve 后的 db_path, as_of_iso, days)``——``"a.db"`` 与
    ``"./a.db"`` 指向同一库时共享缓存；显式传入 ``config`` 时以 config 为准，
    但键仍只按 ``days`` 记——同一个 ``(db, as_of)`` 不要混用不同窗口的 config，
    否则会互相顶掉。
    """
    key = (str(Path(db_path).resolve()), as_of_iso, days)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit, _CACHE_HIT_MS

    cfg = config if config is not None else DetectConfig(window_days=days)
    started = time.perf_counter()
    result = detect(db_path, as_of=as_of_iso, config=cfg)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)

    if cache is not None:
        cache[key] = result
    return result, elapsed_ms


def _matches_scope(point: AnomalyPoint, dim: str, value: str) -> bool:
    """判断一个异常点是否落在该 scope 上。

    ``("all", "全店")`` 匹配全店级点（``dimension`` / ``dim_value`` 都是 None）。
    """
    if dim == "all":
        return point.dimension is None and point.dim_value is None
    return point.dimension == dim and str(point.dim_value) == str(value)


def _point_payload(point: AnomalyPoint) -> dict[str, Any]:
    """卡片视图里的单条证据：只留判定要用的字段。"""
    return {
        "metric": point.metric,
        "current": point.current,
        "baseline": point.baseline,
        "delta_pct": round(point.delta_pct, 4),
        "z": round(point.z, 4),
        "score": round(point.score, 4),
    }


# ---------------------------------------------------------------- 对外接口


def recheck_scope(
    db_path: str,
    scope: tuple[str, str],
    *,
    as_of: str | date,
    required_metric: str | None = None,
    days: int = DEFAULT_RECHECK_DAYS,
    config: Any = None,
    cache: dict[Any, Any] | None = None,
) -> ScopeRecheck:
    """复检一个 scope。

    ``scope`` 形如 ``("sku_id", "SKU0292")``；``("all", "全店")`` 表示全店级。

    * ``scope_found``：宽窗口里是否还有落在这个 scope 上的异常点（双向都算）。
    * ``required_found``：``required_metric`` 是否出现在该 scope 的指标集合里；
      未给 ``required_metric`` 时为 ``False``（调用方据此走 demote 分支）。
    * ``points``：该 scope 的异常点（metric/current/baseline/delta_pct/z/score），
      按 ``score`` 降序、最多 8 条。

    实现说明：``metrics`` 与 ``required_found`` 取该 scope 的**全部**异常点
    （不受 ``points`` 的 8 条上限影响）；``points`` 里只有
    ``delta_pct`` / ``z`` / ``score`` 保留 4 位小数，``current`` / ``baseline``
    保留原值。异常（如库不存在）不吞，直接抛给调用方。
    """
    as_of_iso = _normalize_as_of(as_of)
    dim, value = str(scope[0]), str(scope[1])
    result, elapsed_ms = _detect_cached(db_path, as_of_iso, days, config, cache)

    matched = [p for p in result.points if _matches_scope(p, dim, value)]
    metrics = sorted({p.metric for p in matched})
    ranked = sorted(matched, key=lambda p: (p.score, abs(p.z)), reverse=True)[:_MAX_POINTS]

    return ScopeRecheck(
        scope=(dim, value),
        days=days,
        as_of=as_of_iso,
        scope_found=bool(matched),
        required_metric=required_metric,
        required_found=required_metric is not None and required_metric in metrics,
        metrics=metrics,
        points=[_point_payload(p) for p in ranked],
        elapsed_ms=elapsed_ms,
    )


def recheck_many(
    db_path: str,
    scopes: Sequence[tuple[str, str]],
    *,
    as_of: str | date,
    required: Mapping[tuple[str, str], str | None] | None = None,
    days: int = DEFAULT_RECHECK_DAYS,
    config: Any = None,
    cache: dict[Any, Any] | None = None,
) -> dict[tuple[str, str], ScopeRecheck]:
    """批量复检，返回 ``{(dim, value): ScopeRecheck}``。

    去重：``scopes`` 里重复的项只算一次；顺序无关。
    ``required`` 给出每个 scope 需要的辅证指标，缺省按 ``None`` 处理。

    同一个 ``(db_path, as_of, days)`` 在一次调用里只会真算一次：调用方没给
    ``cache`` 时用本次调用内部的临时缓存兜底（不跨调用保留）。
    """
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for scope in scopes:
        key = (str(scope[0]), str(scope[1]))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)

    local_cache: dict[Any, Any] = cache if cache is not None else {}
    out: dict[tuple[str, str], ScopeRecheck] = {}
    for key in ordered:
        required_metric = required.get(key) if required is not None else None
        out[key] = recheck_scope(
            db_path,
            key,
            as_of=as_of,
            required_metric=required_metric,
            days=days,
            config=config,
            cache=local_cache,
        )
    return out
