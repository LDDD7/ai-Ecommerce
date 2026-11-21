"""异常检测：当前窗口 vs 基线窗口，按维度扫描可疑变化。

为什么不用「阈值告警」而用双窗口 + 显著性
- 单点阈值在 300 个 SKU 上必然误报一片；这里每个候选点都要同时满足
  相对变化、统计显著、绝对变化、样本量四道门槛。
- 统计量用「日均值之差的标准化」，计数类加泊松下限 sqrt(mean)，
  避免基线窗口恰好零方差时 z 变成无穷大。

边界
- 只读：所有取数走 metrics.compiler（QuerySpec → 参数化 SQL → mode=ro）。
- 不做根因判断：这里只回答「哪里变了」，解释交给 decompose/contribution。
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable, Sequence

from app.metrics.compiler import run
from app.metrics.spec import Filter, MetricError, QuerySpec, TimeRange, load_catalog

# 每个源参与巡检的指标（catalog.yaml 里的名字）
CORE_METRICS: dict[str, tuple[str, ...]] = {
    "traffic": ("impressions", "clicks", "ctr", "detail_uv", "add_cart_rate", "cvr", "pay_buyers"),
    "orders": ("gmv", "paid_orders", "aov", "unit_price", "refund_rate"),
    "ads": ("ad_spend", "roi", "cpc"),
    "reviews": ("neg_review_rate", "dsr", "review_cnt"),
    "inventory": ("stockout_rate",),
}

# 只用于当分母/样本量、本身不作为异常上报的指标
SKIP_METRICS = {"review_cnt"}

# 绝对变化下限（单位与指标一致；ratio 是小数，0.005 = 0.5pp）
MIN_ABS: dict[str, float] = {
    "ctr": 0.005,
    "cvr": 0.005,
    "add_cart_rate": 0.01,
    "refund_rate": 0.01,
    "return_rate": 0.005,
    "neg_review_rate": 0.05,
    "stockout_rate": 0.05,
    "roi": 0.1,
    "gross_margin": 0.01,
    "dsr": 0.2,
    "aov": 5.0,
    "unit_price": 5.0,
    "cpc": 0.5,
    "gmv": 200.0,
    "ad_spend": 20.0,
}

# 基线日均低于该值时整格跳过（噪声太大）
MIN_BASE_DAILY: dict[str, float] = {
    "impressions": 200.0,
    "clicks": 20.0,
    "detail_uv": 20.0,
    "pay_buyers": 2.0,
    "paid_orders": 2.0,
    "gmv": 200.0,
    "ad_spend": 10.0,
    "aov": 20.0,
    "unit_price": 20.0,
    "cpc": 0.1,
}

SUM_LIKE = {"impressions", "clicks", "detail_uv", "add_cart_uv", "pay_buyers", "paid_orders", "gmv", "ad_spend"}

# 比率类指标的分母（用于两比例 z 检验，避免小样本格子刷出假异常）
DENOMINATOR: dict[str, str] = {
    "ctr": "impressions",
    "cvr": "detail_uv",
    "add_cart_rate": "detail_uv",
    "refund_rate": "paid_orders",
    "return_rate": "paid_orders",
    "gross_margin": "paid_orders",
    "roi": "ad_spend",
    "cpc": "clicks",
    "neg_review_rate": "review_cnt",
}
# 只有 0~1 之间的比率才适合两比例检验（roi/cpc/dsr 不是）
PROPORTION_METRICS = {
    "ctr",
    "cvr",
    "add_cart_rate",
    "refund_rate",
    "return_rate",
    "gross_margin",
    "neg_review_rate",
}
MIN_DEN_BASE = 50.0
MIN_DEN_CUR = 20.0
# 评价量小得多（单 SKU 单周十几条），单独放宽；不设门槛时「1 条差评」就能刷出巨大的 z。
MIN_DEN_BY_METRIC: dict[str, tuple[float, float]] = {
    "neg_review_rate": (30.0, 12.0),
}
# 稀有事件：退款（r4/r10）与差评（r6）在单 SKU 单周只有几单，分母过小时
# 1 单之差就是 +100%~+400%。除了分母门槛，还要求「事件条数」本身够多。
RARE_EVENT_METRICS = {"refund_rate", "return_rate", "neg_review_rate"}
MIN_EVENTS_BASE = 2.0
MIN_EVENTS_CUR = 3.0
# 稀有事件的两比例 z 也不可靠（几单的波动就能到 2.0~2.6），单独抬高门槛到 2.2。
RARE_Z_MIN = 2.2
# 促销失衡阈值：0.15 时「7 天窗口里有一天大促」就会把全部指标降权（score×0.25、
# z 门槛提到 3.0），实测把 10-20 的真实退款异常一起压掉了；0.35 只拦真正不可比的窗口。
PROMO_SKEW_THRESHOLD = 0.35


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days}


@dataclass(frozen=True)
class DetectConfig:
    window_days: int = 7
    baseline_days: int = 28
    min_rel: float = 0.15
    z_min: float = 2.0
    min_base_days: int = 14
    min_cur_days: int = 4
    max_per_dim: int = 30
    per_metric_dim_cap: int = 3
    promo_z_min: float = 3.0
    # 促销口径：窗口与基线都只取「与大促状态相同」的天（含/不含大促），
    # 否则「窗口在双 11、基线在平销」的对比会把促销涨幅全算成异常。
    match_promo: bool = False
    min_matched_days: int = 7
    promo_lookback_days: int = 56
    dims: tuple[str, ...] = ("style", "channel_id", "sku_id")
    top_metric: str = "paid_orders"


@dataclass
class AnomalyPoint:
    metric: str
    display: str
    unit: str
    dimension: str | None
    dim_value: str | None
    current: float
    baseline: float
    delta: float
    delta_pct: float
    z: float
    direction: str
    score: float
    severity: str
    n_cur: int
    n_base: int
    window: Window
    baseline_window: Window
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "display": self.display,
            "unit": self.unit,
            "dimension": self.dimension,
            "dim_value": self.dim_value,
            "current": round(self.current, 6),
            "baseline": round(self.baseline, 6),
            "delta": round(self.delta, 6),
            "delta_pct": round(self.delta_pct, 6),
            "z": round(self.z, 3),
            "direction": self.direction,
            "score": round(self.score, 4),
            "severity": self.severity,
            "n_cur": self.n_cur,
            "n_base": self.n_base,
            "flags": list(self.flags),
            "window": self.window.as_dict(),
            "baseline_window": self.baseline_window.as_dict(),
        }


@dataclass
class DetectionResult:
    as_of: date
    window: Window
    baseline: Window
    points: list[AnomalyPoint] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def top(self, n: int = 10) -> list[AnomalyPoint]:
        return self.points[:n]

    def by_dimension(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.points:
            key = p.dimension or "all"
            out[key] = out.get(key, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "window": self.window.as_dict(),
            "baseline_window": self.baseline.as_dict(),
            "points": [p.as_dict() for p in self.points],
            "meta": self.meta,
        }


# ---------------------------------------------------------------- 取数


def resolve_as_of(db_path: Path | str) -> date:
    """库里最新一天（有订单的最后一天）。"""
    path = Path(db_path)
    if not path.exists():
        raise MetricError(f"数据库不存在: {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute("SELECT MAX(dt) FROM fact_orders").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise MetricError("fact_orders 为空，请先跑 app.data.seed")
    return date.fromisoformat(row[0])


def _daily(
    db_path: Path | str,
    metrics: Sequence[str],
    dim: str | None,
    window: Window,
    filters: Sequence[Filter] = (),
) -> dict[str | None, dict[str, list[tuple[date, float]]]]:
    """按 (维度值, 指标) 取每日序列。维度为 None 时只有全店一条。

    QuerySpec 单次最多 6 个指标，这里按 6 个一组切分后合并。
    """
    dims = [dim, "dt"] if dim else ["dt"]
    out: dict[str | None, dict[str, list[tuple[date, float]]]] = {}

    names = list(metrics)
    for i in range(0, len(names), 6):
        chunk = names[i : i + 6]
        spec = QuerySpec(
            metrics=chunk,
            dimensions=dims,
            time=TimeRange(start=window.start, end=window.end),
            filters=list(filters),
            limit=5000,
        )
        res = run(spec, db_path)
        idx = {c: j for j, c in enumerate(res.columns)}
        i_dt = idx["dt"]
        i_dim = idx[dim] if dim else None
        for row in res.rows:
            cell = row[i_dim] if i_dim is not None else None
            d = date.fromisoformat(row[i_dt])
            bucket = out.setdefault(cell, {})
            for m in chunk:
                v = row[idx[m]]
                if v is None:
                    continue
                bucket.setdefault(m, []).append((d, float(v)))
    return out


def top_values(
    db_path: Path | str,
    dim: str,
    window: Window,
    n: int,
    metric: str = "paid_orders",
) -> list[str]:
    """按指标总量取该维度的前 n 个取值（用于把 300 个 SKU 收窄到 30 个）。"""
    cat = load_catalog()
    if metric not in cat.names():
        metric = "gmv"
    if dim not in cat.get(metric).allowed_dims:
        for cand in ("paid_orders", "gmv", "impressions", "detail_uv"):
            if cand in cat.names() and dim in cat.get(cand).allowed_dims:
                metric = cand
                break
        else:
            return []
    spec = QuerySpec(
        metrics=[metric],
        dimensions=[dim],
        time=TimeRange(start=window.start, end=window.end),
        order_by=[{"field": metric, "dir": "desc"}],
        limit=n,
    )
    res = run(spec, db_path)
    i_dim = res.columns.index(dim)
    return [str(r[i_dim]) for r in res.rows if r[i_dim] is not None]


# ---------------------------------------------------------------- 判定


def _stats(
    metric: str,
    unit: str,
    base_vals: Sequence[float],
    cur_vals: Sequence[float],
) -> tuple[float, float, float, float]:
    """返回 (cur_mean, base_mean, delta, z)。"""
    base_mean = mean(base_vals)
    cur_mean = mean(cur_vals)
    sd = pstdev(base_vals) if len(base_vals) > 1 else 0.0
    if metric in SUM_LIKE or unit == "count":
        sd = max(sd, math.sqrt(max(base_mean, 0.0)))
    se = sd * math.sqrt(1.0 / len(cur_vals) + 1.0 / len(base_vals))
    delta = cur_mean - base_mean
    if se <= 1e-12:
        z = 0.0 if abs(delta) <= 1e-9 else 99.0
    else:
        z = delta / se
    return cur_mean, base_mean, delta, z


def _cell_change(
    metric: str,
    unit: str,
    per_metric: dict[str, list[tuple[date, float]]],
    baseline: Window,
    window: Window,
    cfg: DetectConfig,
    *,
    cur_days: set[date] | None = None,
    base_days: set[date] | None = None,
) -> tuple[float, float, float, float, int, int] | None:
    """单格变化量。比率类优先用两比例 z 检验（需要分母），否则回退到均值检验。

    `cur_days` / `base_days` 是「实际参与比较的日期集合」（促销口径匹配后用），
    None 表示用整个窗口。
    """
    rows = per_metric.get(metric) or []
    base_sel = base_days if base_days is not None else None
    cur_sel = cur_days if cur_days is not None else None

    def _in_base(d: date) -> bool:
        return d in base_sel if base_sel is not None else baseline.contains(d)

    def _in_cur(d: date) -> bool:
        return d in cur_sel if cur_sel is not None else window.contains(d)

    base_vals = [v for d, v in rows if _in_base(d)]
    cur_vals = [v for d, v in rows if _in_cur(d)]
    need_base = cfg.min_base_days if base_days is None else min(cfg.min_base_days, max(len(base_days), cfg.min_cur_days))
    if len(base_vals) < need_base or len(cur_vals) < cfg.min_cur_days:
        return None
    if mean(base_vals) < MIN_BASE_DAILY.get(metric, 0.0):
        return None

    den_name = DENOMINATOR.get(metric)
    if metric in PROPORTION_METRICS and den_name and den_name in per_metric:
        den = dict(per_metric[den_name])
        pairs_b = [(v, den[d]) for d, v in rows if _in_base(d) and d in den]
        pairs_c = [(v, den[d]) for d, v in rows if _in_cur(d) and d in den]
        tot_b = sum(x for _, x in pairs_b)
        tot_c = sum(x for _, x in pairs_c)
        min_base, min_cur = MIN_DEN_BY_METRIC.get(metric, (MIN_DEN_BASE, MIN_DEN_CUR))
        if tot_b >= min_base and tot_c >= min_cur:
            nb = sum(r * x for r, x in pairs_b)
            nc = sum(r * x for r, x in pairs_c)
            # 稀有事件（退款/差评）在小分母下 1 单之差就能造出 +400%，
            # 必须同时要求「事件条数」够多，否则评分会被噪声主导。
            if metric in RARE_EVENT_METRICS and (nb < MIN_EVENTS_BASE or nc < MIN_EVENTS_CUR):
                return None
            pb = nb / tot_b
            pc = nc / tot_c
            pooled = min(max((nb + nc) / (tot_b + tot_c), 0.0), 1.0)
            se = math.sqrt(max(pooled * (1 - pooled), 1e-12) * (1 / tot_b + 1 / tot_c))
            z = (pc - pb) / se if se > 0 else 0.0
            return pc, pb, pc - pb, z, len(cur_vals), len(base_vals)

    cur_mean, base_mean, delta, z = _stats(metric, unit, base_vals, cur_vals)
    return cur_mean, base_mean, delta, z, len(cur_vals), len(base_vals)


def _promo_days(db_path: Path | str, start: date, end: date) -> set[date]:
    """取区间内的大促日集合（读 dim_date，只用于口径匹配，不算指标）。"""
    path = Path(db_path)
    if not path.exists():
        return set()
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT dt FROM dim_date WHERE dt BETWEEN ? AND ? AND is_promo = 1",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        con.close()
    return {date.fromisoformat(r[0]) for r in rows if r and r[0]}


def _matched_days(
    all_days: list[date],
    promo_days: set[date],
    *,
    want_promo: bool,
) -> list[date]:
    """从候选日里挑出与目标促销状态一致的天。"""
    return [d for d in all_days if (d in promo_days) == want_promo]


def _promo_share(db_path: Path | str, window: Window) -> float:
    """窗口内「大促日订单占比」，用于识别「窗口在促销、基线不在」的伪异常。"""
    try:
        spec = QuerySpec(
            metrics=["paid_orders"],
            dimensions=["is_promo"],
            time=TimeRange(start=window.start, end=window.end),
            limit=10,
        )
        res = run(spec, db_path)
    except MetricError:
        return 0.0
    i_flag = res.columns.index("is_promo")
    i_val = res.columns.index("paid_orders")
    total = promo = 0.0
    for row in res.rows:
        v = float(row[i_val] or 0.0)
        total += v
        if int(row[i_flag] or 0) == 1:
            promo += v
    return promo / total if total else 0.0


def _promo_share_days(db_path: Path | str, days: set[date]) -> float:
    """给定日期集合里的大促日订单占比（用于促销口径匹配后的真实失衡度）。"""
    if not days:
        return 0.0
    path = Path(db_path)
    if not path.exists():
        return 0.0
    marks = ",".join("?" for _ in days)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute(
            f"SELECT SUM(CASE WHEN d.is_promo = 1 THEN 1 ELSE 0 END), COUNT(*)"
            f" FROM dim_date d WHERE d.dt IN ({marks})",
            [d.isoformat() for d in sorted(days)],
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    finally:
        con.close()
    if not row or not row[1]:
        return 0.0
    return float(row[0] or 0) / float(row[1])


def _severity(delta_pct: float, z: float) -> str:
    a = abs(delta_pct)
    if a >= 0.5 or abs(z) >= 4.0:
        return "high"
    if a >= 0.3 or abs(z) >= 3.0:
        return "medium"
    return "low"


def detect(
    db_path: Path | str = "data/store.db",
    as_of: date | str | None = None,
    config: DetectConfig | None = None,
) -> DetectionResult:
    """跑一轮巡检，返回按严重度排序的候选异常点。"""
    t0 = time.perf_counter()
    cfg = config or DetectConfig()
    cat = load_catalog()
    as_of_d = as_of if isinstance(as_of, date) else date.fromisoformat(as_of) if as_of else resolve_as_of(db_path)

    window = Window(as_of_d - timedelta(days=cfg.window_days - 1), as_of_d)
    baseline = Window(window.start - timedelta(days=cfg.baseline_days), window.start - timedelta(days=1))
    span = Window(baseline.start, window.end)

    # 扫描目标：全店 + 各维度（SKU 先按量收窄）
    targets: list[tuple[str | None, list[Filter]]] = [(None, [])]
    scanned: dict[str, int] = {"all": 1}
    for dim in cfg.dims:
        vals = top_values(db_path, dim, baseline, cfg.max_per_dim, cfg.top_metric)
        if len(vals) < 2:
            continue
        filters = [Filter(field=dim, op="in", value=vals)] if dim == "sku_id" else []
        targets.append((dim, filters))
        scanned[dim] = len(vals)

    promo_cur = _promo_share(db_path, window)
    promo_base = _promo_share(db_path, baseline)
    promo_skew = promo_cur - promo_base

    # 促销口径匹配：窗口与基线只取「与大促状态相同」的天。
    # 10-27 那种「整周在双 11」的窗口只能靠这条救：否则促销涨幅会把真实异常淹掉。
    cur_days: set[date] | None = None
    base_days: set[date] | None = None
    promo_matched = False
    promo_fallback = ""
    if cfg.match_promo:
        span_promo = _promo_days(db_path, baseline.start - timedelta(days=cfg.promo_lookback_days), window.end)
        want_promo = promo_cur >= 0.5
        win_all = [window.start + timedelta(days=i) for i in range(window.days)]
        win_sel = _matched_days(win_all, span_promo, want_promo=want_promo)
        back_start = window.start - timedelta(days=cfg.promo_lookback_days)
        look_all = [back_start + timedelta(days=i) for i in range((window.start - back_start).days)]
        base_sel = _matched_days(look_all, span_promo, want_promo=want_promo)[-cfg.baseline_days :]
        if len(win_sel) >= cfg.min_cur_days and len(base_sel) >= cfg.min_matched_days:
            cur_days, base_days = set(win_sel), set(base_sel)
            promo_matched = True
            eval_window = Window(min(win_sel), max(win_sel))
            eval_baseline = Window(min(base_sel), max(base_sel))
            promo_cur = _promo_share_days(db_path, cur_days)
            promo_base = _promo_share_days(db_path, base_days)
            promo_skew = promo_cur - promo_base
        else:
            promo_fallback = (
                f"匹配日不足（窗口 {len(win_sel)} 天 / 基线 {len(base_sel)} 天）"
            )

    if cur_days is None:
        eval_window, eval_baseline = window, baseline
    # 匹配到的基线可能超出原来的 28 天（要往前找够同促销状态的天），取数范围跟着扩
    span = Window(min(eval_baseline.start, baseline.start), window.end)

    found: dict[tuple[str, str | None, str | None], AnomalyPoint] = {}
    cells_scanned = 0

    for dim, filters in targets:
        for source, names in CORE_METRICS.items():
            metrics = [m for m in names if dim is None or dim in cat.get(m).allowed_dims]
            if not metrics:
                continue
            try:
                series = _daily(db_path, metrics, dim, span, filters)
            except MetricError:
                continue
            for cell, per_metric in series.items():
                if dim is not None and cell is None:
                    continue
                for metric in metrics:
                    if metric in SKIP_METRICS:
                        continue
                    mdef = cat.get(metric)
                    change = _cell_change(
                        metric, mdef.unit, per_metric, baseline, window, cfg,
                        cur_days=cur_days, base_days=base_days,
                    )
                    if change is None:
                        continue
                    cells_scanned += 1
                    cur_mean, base_mean, delta, z, n_cur, n_base = change

                    promo_like = abs(promo_skew) > PROMO_SKEW_THRESHOLD
                    z_min = cfg.promo_z_min if promo_like else cfg.z_min
                    if metric in RARE_EVENT_METRICS:
                        z_min = max(z_min, RARE_Z_MIN)
                    delta_pct = delta / base_mean if base_mean > 0 else 0.0
                    if abs(delta_pct) < cfg.min_rel or abs(z) < z_min:
                        continue
                    if abs(delta) < MIN_ABS.get(metric, 0.0):
                        continue

                    flags: list[str] = []
                    if promo_matched:
                        flags.append("promo_matched")
                    if promo_like:
                        flags.append("promo_skew")
                    # 相对变化封顶在 100%：小基数上的 +450% 不该压过 -37% 的真实下滑，
                    # 量级信息由 z 承担，rel 只负责「业务上可感」。
                    rel_term = min(abs(delta_pct), 1.0)
                    score = rel_term * min(max(abs(z), 1.0), 6.0) / 3.0
                    if promo_like:
                        score *= 0.25
                    point = AnomalyPoint(
                        metric=metric,
                        display=mdef.display,
                        unit=mdef.unit,
                        dimension=dim,
                        dim_value=None if cell is None else str(cell),
                        current=cur_mean,
                        baseline=base_mean,
                        delta=delta,
                        delta_pct=delta_pct,
                        z=z,
                        direction="up" if delta > 0 else "down",
                        score=round(score, 4),
                        severity=_severity(delta_pct, z),
                        n_cur=n_cur,
                        n_base=n_base,
                        window=eval_window,
                        baseline_window=eval_baseline,
                        flags=flags,
                    )
                    key = (metric, dim, point.dim_value)
                    old = found.get(key)
                    if old is None or point.score > old.score:
                        found[key] = point

    ranked = sorted(found.values(), key=lambda p: (p.score, abs(p.z)), reverse=True)
    per_key: dict[tuple[str, str | None], int] = {}
    points: list[AnomalyPoint] = []
    for p in ranked:
        key = (p.metric, p.dimension)
        if per_key.get(key, 0) >= cfg.per_metric_dim_cap:
            continue
        per_key[key] = per_key.get(key, 0) + 1
        points.append(p)

    meta = {
        "engine": "zscore-v1",
        "config": {
            "window_days": cfg.window_days,
            "baseline_days": cfg.baseline_days,
            "min_rel": cfg.min_rel,
            "z_min": cfg.z_min,
            "max_per_dim": cfg.max_per_dim,
        },
        "scanned": scanned,
        "promo_share": {
            "window": round(promo_cur, 4),
            "baseline": round(promo_base, 4),
            "skew": round(promo_skew, 4),
        },
        "promo_match": {
            "enabled": cfg.match_promo,
            "matched": promo_matched,
            "window_days": len(cur_days) if cur_days else window.days,
            "baseline_days": len(base_days) if base_days else baseline.days,
            "fallback": promo_fallback,
        },
        # 整段都在促销里、且基线几乎没有促销天：单周同比不可比（10-27 双 11 就是这种）
        "promo_dominated": promo_cur >= 0.9 and promo_base < 0.3,

        "cells_scanned": cells_scanned,
        "raw_candidates": len(ranked),
        "candidates": len(points),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return DetectionResult(as_of=as_of_d, window=window, baseline=baseline, points=points, meta=meta)


def summarize(result: DetectionResult, limit: int = 10) -> str:
    """人读摘要（mock reporter 也会用）。"""
    lines = [
        f"巡检 {result.as_of}｜窗口 {result.window.start}~{result.window.end} vs 基线 {result.baseline.start}~{result.baseline.end}",
        f"候选 {result.meta.get('candidates', 0)} 条（扫描 {result.meta.get('cells_scanned', 0)} 个格）",
    ]
    for p in result.top(limit):
        where = "全店" if p.dimension is None else f"{p.dimension}={p.dim_value}"
        lines.append(
            f"- {p.display}({where}) {p.baseline:.4g} → {p.current:.4g}"
            f"（{p.delta_pct:+.1%}, z={p.z:.1f}, {p.severity}）"
        )
    return "\n".join(lines)
