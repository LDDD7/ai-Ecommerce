"""诊断套件（diagnose）：从 ``anomaly_gt`` 抽 25 个场景，评根因 top-1 / top-3。

计分口径（冻结，别改）：

- top-1 命中 → ``score = 1.0`` 且 ``ok = True``
- 否则 top-3 命中 → ``score = 0.6`` 且 ``ok = False``
- 否则 → ``score = 0.0``

用例生成是确定性的，只读 ``anomaly_gt`` 与 ``dim_date``：

1. 每个 code 两个 as_of：``min(dt_end, 库内最大日期)`` 与窗口中点
   ``dt_start + (dt_end - dt_start) // 2``；
2. 再按窗口长度 ``dt_end - dt_start`` 降序取前 5 个 code，各补一个
   ``dt_start + (dt_end - dt_start) * 3 // 4``，凑满 25 条；
3. ``(code, as_of)`` 撞车时向后（再向前）顺延，且始终夹在该 code 的窗口内，
   保证这条用例考的还是它自己的根因；
4. 所有 as_of 都夹在 ``dim_date`` 的 min~max 之间。

``expect`` 不是「这一个 code」，而是**当天所有仍落在窗口内的 code**：
R1 与 R10（10-25 附近）、R5 与 R9（10-01~10-10）这些注入窗口本来就重叠，
只认一个真值会把正确答案判成误报，评测就不公平了。

``score_case`` 是纯函数，测试可以直接喂构造好的 pred / gold，不必跑库。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.diagnose.diagnosis import build_diagnosis
from app.eval.metrics import confusion, mean, precision_recall, topk_hit
from app.eval.types import Case, CaseResult, SuiteResult

SUITE = "diagnose"

CASE_COUNT = 25
# 前 5 个「窗口最长」的 code 各多补一条 as_of
EXTRA_CASE_CODES = 5
TOP1_SCORE = 1.0
TOP3_SCORE = 0.6
# 与 build_diagnosis 默认窗口一致：用例的 as_of 是「看哪一天」，窗口由诊断入口决定。
WINDOW_DAYS = 7


# ---------------------------------------------------------------- 读库


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """只读连接：生成用例不该有任何写库的可能。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"找不到数据库：{p}")
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def _as_date(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _code_key(code: str) -> tuple[int, int, str]:
    """按 R1, R2, …, R10 自然序排（字符串序会把 R10 排到 R2 前面）。"""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if digits:
        return (0, int(digits), str(code))
    return (1, 0, str(code))


def _load_windows(
    db_path: str | Path,
) -> tuple[dict[str, list[tuple[date, date]]], dict[str, str], dict[str, str], list[str], date, date]:
    """读全部注入窗口。

    返回 ``(intervals, spans, labels, codes, db_min, db_max)``：

    - ``intervals`` 每个 code 的窗口列表（同一 code 多行时都保留，用于日期包含判断）
    - ``spans``     每个 code 的合并窗口（min 起 / max 止），用于选 as_of
    - ``labels``    code → 中文短名
    """
    con = _connect_ro(db_path)
    try:
        cols = {str(row[1]) for row in con.execute("PRAGMA table_info(anomaly_gt)")}
        if not cols:
            raise ValueError("库里没有 anomaly_gt 表，无法生成诊断用例")
        # 真库里中文短名在 name 列；按任务书里更精简的表结构则退化成 root_cause。
        label_col = "name" if "name" in cols else "root_cause"
        rows = con.execute(
            f"SELECT code, {label_col}, dt_start, dt_end FROM anomaly_gt ORDER BY code, dt_start, dt_end"
        ).fetchall()
        bounds = con.execute("SELECT MIN(dt), MAX(dt) FROM dim_date").fetchone()
    finally:
        con.close()

    if not rows:
        raise ValueError("anomaly_gt 为空，无法生成诊断用例")
    if not bounds or not bounds[0] or not bounds[1]:
        raise ValueError("dim_date 为空，无法确定用例的日期范围")
    db_min, db_max = _as_date(bounds[0]), _as_date(bounds[1])

    intervals: dict[str, list[tuple[date, date]]] = {}
    labels: dict[str, str] = {}
    for code_raw, label_raw, start_raw, end_raw in rows:
        code = str(code_raw or "").strip().upper()
        if not code:
            continue
        start, end = _as_date(start_raw), _as_date(end_raw)
        if end < start:
            start, end = end, start
        # 与库内区间无交集的窗口没法评，直接跳过（不硬造用例）
        if end < db_min or start > db_max:
            continue
        intervals.setdefault(code, []).append((start, end))
        labels.setdefault(code, str(label_raw or code).strip() or code)

    if not intervals:
        raise ValueError("anomaly_gt 的窗口都不在 dim_date 范围内")

    spans = {code: (min(s for s, _ in ivs), max(e for _, e in ivs)) for code, ivs in intervals.items()}
    codes = sorted(intervals, key=_code_key)
    return intervals, spans, labels, codes, db_min, db_max


# ---------------------------------------------------------------- 选 as_of


def _as_of_candidates(
    codes: Sequence[str],
    spans: dict[str, tuple[date, date]],
    db_max: date,
) -> list[tuple[str, date]]:
    """按任务书规则产出 20 条基础 + 5 条补充的 ``(code, as_of)``。"""
    out: list[tuple[str, date]] = []
    for code in codes:
        start, end = spans[code]
        out.append((code, min(end, db_max)))  # ① 窗口结束（夹到库末日）
        out.append((code, start + timedelta(days=(end - start).days // 2)))  # ② 窗口中点

    longest = sorted(
        codes,
        key=lambda c: (-(spans[c][1] - spans[c][0]).days, _code_key(c)),
    )[:EXTRA_CASE_CODES]
    for code in longest:
        start, end = spans[code]
        out.append((code, start + timedelta(days=(end - start).days * 3 // 4)))  # ③ 后 1/4
    return out


def _free_day(
    day: date,
    used: set[date],
    span: tuple[date, date],
    db_min: date,
    db_max: date,
) -> date:
    """撞车顺延：先在窗口内向后找，再向前找；窗口内实在没有才扩到全库。"""
    lo, hi = max(span[0], db_min), min(span[1], db_max)
    cursor = day + timedelta(days=1)
    while cursor <= hi:
        if cursor not in used:
            return cursor
        cursor += timedelta(days=1)
    cursor = day - timedelta(days=1)
    while cursor >= lo:
        if cursor not in used:
            return cursor
        cursor -= timedelta(days=1)
    # 兜底：窗口只有一天（start == end）时无处可让，只能出窗口。
    cursor = db_min
    while cursor <= db_max:
        if cursor not in used:
            return cursor
        cursor += timedelta(days=1)
    raise ValueError(f"{day} 所在窗口没有可用的 as_of")


def _materialize(
    candidates: Iterable[tuple[str, date]],
    spans: dict[str, tuple[date, date]],
    db_min: date,
    db_max: date,
) -> list[tuple[str, date]]:
    """夹日期 + 按 (code, as_of) 去重顺延；顺序不变，保证 case_id 稳定。"""
    used: dict[str, set[date]] = {}
    out: list[tuple[str, date]] = []
    for code, day in candidates:
        day = min(max(day, db_min), db_max)
        seen = used.setdefault(code, set())
        if day in seen:
            day = _free_day(day, seen, spans[code], db_min, db_max)
        seen.add(day)
        out.append((code, day))
    return out


def _covering_codes(intervals: dict[str, list[tuple[date, date]]], day: date) -> list[str]:
    """当天仍在窗口内的所有 code（日期区间包含，含端点）。"""
    return sorted(
        (code for code, ivs in intervals.items() if any(start <= day <= end for start, end in ivs)),
        key=_code_key,
    )


# ---------------------------------------------------------------- 用例


def cases(db_path: str | Path) -> list[Case]:
    """生成 25 条诊断用例（确定性；只读库，不跑诊断）。"""
    intervals, spans, labels, codes, db_min, db_max = _load_windows(db_path)
    pairs = _materialize(
        _as_of_candidates(codes, spans, db_max),
        spans,
        db_min,
        db_max,
    )

    out: list[Case] = []
    for idx, (code, day) in enumerate(pairs, start=1):
        gold = _covering_codes(intervals, day)
        if code not in gold:
            # 理论上不可达（顺延被夹在窗口内）；真到了这一步，也要保证这条用例
            # 考的是它自己的根因，否则用例失去意义。
            gold = sorted(set(gold) | {code}, key=_code_key)
        start, end = spans[code]
        out.append(
            Case(
                case_id=f"D{idx:02d}",
                suite=SUITE,
                payload={
                    "label": labels[code],
                    "as_of": day.isoformat(),
                    "expect": gold,
                    "window": [start.isoformat(), end.isoformat()],
                },
                note=(
                    f"来自 anomaly_gt 的 {code} 窗口 "
                    f"{start.isoformat()}~{end.isoformat()}（{labels[code]}）"
                ),
            )
        )

    if len(out) != CASE_COUNT:
        raise ValueError(
            f"anomaly_gt 只能生成 {len(out)} 条用例（需要 {CASE_COUNT} 条）"
            "：检查库里的 code 是否覆盖 R1~R10"
        )
    return out


# ---------------------------------------------------------------- 计分


def score_case(
    pred_codes: Sequence[str] | None,
    gold_codes: Iterable[str] | None,
) -> tuple[bool, float, dict[str, Any]]:
    """top-1 / top-3 计分（纯函数）。

    - top-1 命中 → ``(True, 1.0, detail)``
    - 否则 top-3 命中 → ``(False, 0.6, detail)``
    - 否则 ``(False, 0.0, detail)``

    ``detail`` 带 ``top1_hit`` / ``top3_hit`` / ``precision`` / ``recall`` /
    ``confusion``（多真值时精度按集合算：命中的真值数 / 预测去重后的条数）。
    """
    pred = [str(c) for c in (pred_codes or []) if c]
    gold = [str(c) for c in (gold_codes or []) if c]
    gold_set = set(gold)

    precision, recall = precision_recall(pred, gold)
    top1_hit = bool(pred) and pred[0] in gold_set
    top3_hit = topk_hit(pred, gold, k=3)

    if top1_hit:
        ok, score, level = True, TOP1_SCORE, "top1"
    elif top3_hit:
        ok, score, level = False, TOP3_SCORE, "top3"
    else:
        ok, score, level = False, 0.0, "miss"

    detail: dict[str, Any] = {
        "top1_hit": top1_hit,
        "top3_hit": top3_hit,
        "precision": precision,
        "recall": recall,
        "confusion": confusion(pred, gold),
        "level": level,
    }
    return ok, score, detail


# ---------------------------------------------------------------- 跑套件


def _card_brief(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": card.get("code"),
        "title": card.get("title"),
        "confidence": card.get("confidence"),
        "gmv_impact": card.get("gmv_impact"),
    }


def _run_case(db_path: str | Path, case: Case) -> CaseResult:
    payload = case.payload
    as_of = str(payload.get("as_of") or "")
    gold = [str(c) for c in (payload.get("expect") or [])]

    cards: list[dict[str, Any]] = []
    pred: list[str] = []
    error: str | None = None
    t0 = time.perf_counter()
    try:
        report = build_diagnosis(db_path, as_of=as_of, with_replan=True)
        cards = [c for c in (report.get("cards") or []) if isinstance(c, dict)]
        pred = [str(c.get("code") or "") for c in cards]
        ok, score, detail = score_case(pred, gold)
    except Exception as exc:  # noqa: BLE001 - 单条用例失败不该中断整个套件
        error = f"{type(exc).__name__}: {exc}"
        ok, score = False, 0.0
        detail = {
            "top1_hit": False,
            "top3_hit": False,
            "precision": 0.0,
            "recall": 0.0,
            "confusion": {"hit": 0, "extra": 0, "missed": len(set(gold))},
            "level": "error",
            "error": error,
        }
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    detail.update(
        {
            "as_of": as_of,
            "label": payload.get("label"),
            "window": list(payload.get("window") or []),
            "pred": pred,
            "gold": gold,
            "cards": [_card_brief(c) for c in cards],
            "elapsed_ms": elapsed_ms,
            "error": error,
        }
    )
    return CaseResult(case_id=case.case_id, ok=ok, score=score, detail=detail)


def _aggregate(results: Sequence[CaseResult]) -> dict[str, Any]:
    """套件级汇总；precision/recall 是逐条算完再平均，不是全局合并。"""
    details = [r.detail for r in results]
    return {
        "engine": "rules-v1",
        "cases": len(results),
        "top1_rate": round(mean(1.0 if d.get("top1_hit") else 0.0 for d in details), 4),
        "top3_rate": round(mean(1.0 if d.get("top3_hit") else 0.0 for d in details), 4),
        "precision": round(mean(float(d.get("precision") or 0.0) for d in details), 4),
        "recall": round(mean(float(d.get("recall") or 0.0) for d in details), 4),
        "avg_cards": round(mean(len(d.get("pred") or []) for d in details), 2),
        "avg_ms": round(mean(float(d.get("elapsed_ms") or 0.0) for d in details), 1),
    }


def run(db_path: str | Path, *, limit: int | None = None) -> SuiteResult:
    """逐条跑 ``build_diagnosis`` 并计分；``limit`` 只用于冒烟（别在测试里跑全量）。"""
    t0 = time.perf_counter()
    try:
        built = cases(db_path)
    except Exception as exc:  # noqa: BLE001
        return SuiteResult(
            suite=SUITE,
            cases=[],
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            meta=_aggregate([]),
            error=f"{type(exc).__name__}: {exc}",
        )

    selected = built if limit is None else built[: max(0, int(limit))]
    results = [_run_case(db_path, case) for case in selected]
    return SuiteResult(
        suite=SUITE,
        cases=results,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        meta=_aggregate(results),
    )


__all__ = ["SUITE", "CASE_COUNT", "cases", "run", "score_case"]
