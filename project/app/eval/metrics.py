"""评测用的纯函数计分工具（无 IO、可单测）。

刻意保持「不依赖任何套件」：query / diagnose / action / vision 四个套件
都只用这里的函数做判定，避免各写一套导致口径漂移。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence


def ratio(n: float, d: float) -> float:
    """安全除法；分母为 0 时返回 0.0。"""
    return 0.0 if not d else float(n) / float(d)


def mean(values: Iterable[float]) -> float:
    xs = list(values)
    return 0.0 if not xs else sum(xs) / len(xs)


def numeric_close(a: Any, b: Any, *, rel: float = 1e-6, abs_: float = 1e-6) -> bool:
    """数值相等（含相对/绝对容差）；非数值一律判否。"""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if fa == fb:
        return True
    return abs(fa - fb) <= max(abs_, rel * max(abs(fa), abs(fb)))


def subset_match(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """expected 的每个键都要在 actual 里出现且相等（数值用容差）。

    用来做「问数四项全对」：expected 里只写要考的四项（指标、维度、时间、过滤），
    实际结果多出来的键不扣分。
    """
    for key, want in expected.items():
        if key not in actual:
            return False
        got = actual[key]
        if isinstance(want, dict):
            if not isinstance(got, dict) or not subset_match(got, want):
                return False
        elif isinstance(want, (list, tuple)):
            if list(got) != list(want):
                return False
        elif isinstance(want, (int, float)) and not isinstance(want, bool):
            if not numeric_close(got, want):
                return False
        elif got != want:
            return False
    return True


def precision_recall(pred: Sequence[str], gold: Iterable[str]) -> tuple[float, float]:
    """集合口径的精确率 / 召回率（重复项按集合去重）。"""
    pred_set = {p for p in pred if p}
    gold_set = {g for g in gold if g}
    if not pred_set or not gold_set:
        return 0.0, 0.0
    hit = len(pred_set & gold_set)
    return hit / len(pred_set), hit / len(gold_set)


def topk_hit(pred: Sequence[str], gold: Iterable[str], k: int = 3) -> bool:
    """前 k 条预测里是否命中任一真根因。"""
    gold_set = {g for g in gold if g}
    return any(p in gold_set for p in list(pred)[:k])


def range_score(value: float, low: float, high: float) -> float:
    """落在 [low, high] 内得 1 分，否则按距离线性衰减到 0（high-low 为尺度）。"""
    span = max(high - low, 1e-9)
    if low <= value <= high:
        return 1.0
    distance = low - value if value < low else value - high
    return max(0.0, 1.0 - distance / span)


def clamp01(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else float(value)


def weighted_score(parts: dict[str, float], weights: dict[str, float]) -> float:
    """加权得分；缺失的分项按 0 计入，权重和归一。"""
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0.0
    return clamp01(sum(parts.get(k, 0.0) * w for k, w in weights.items()) / total_weight)


def confusion(pred: Sequence[str], gold: Iterable[str]) -> dict[str, int]:
    """命中/误报/漏报计数，报告里用来看「卡多但不对」还是「卡少但准」。"""
    pred_set = {p for p in pred if p}
    gold_set = {g for g in gold if g}
    return {
        "hit": len(pred_set & gold_set),
        "extra": len(pred_set - gold_set),
        "missed": len(gold_set - pred_set),
    }
