"""回归门禁（T7）。

门禁只做一件事：把「套件产出的验收口径」和「阈值」比一比，给出通过/不通过。

阈值口径说明（spec §12.5）：

- 问数、动作是**逐条全对**才算过，所以阈值是 1.0；
- 诊断用三个角度：top-1 命中率、top-3 命中率、逐条精确率。top-3 是产品承诺
  （「前三个根因里有真的」），precision 用来防「堆卡堆出来」；
- 素材是 field-level 准确率（6 个字段逐字段比对），阈值 0.85。

``GATE_DEFAULTS`` 里的数字是**首轮实测后校准**的（不是拍脑袋）：实测值往下留
2~5 个点作为回归余量，避免正常波动就红。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.eval.types import SuiteResult

# 首轮实测（2026-11-21，`data/store.db`，见 docs/eval-report.md）后校准：
#   query 1.000 / diagnose top1 0.440 / top3 0.520 / precision 0.387
#   action 1.000 / vision field 1.000（mock 回放，真机 vlm 见 §19.5）
# 诊断三项离 spec §3.8 的目标（top-3 ≥0.80）还有明显差距，这里**不**把阈值定到
# 目标值——门禁是防回归的，不是许愿池；差距写在报告里，谁改引擎谁负责往上推。
GATE_DEFAULTS: dict[str, float] = {
    "query.accuracy": 1.00,
    "diagnose.top1_rate": 0.40,
    "diagnose.top3_rate": 0.48,
    "diagnose.precision": 0.35,
    "action.accuracy": 1.00,
    "vision.field_accuracy": 0.90,
}

GATE_DESCRIPTIONS: dict[str, str] = {
    "query.accuracy": "问数：20 条全部与独立 SQL 对账一致",
    "diagnose.top1_rate": "诊断：首位根因命中率",
    "diagnose.top3_rate": "诊断：前三个根因至少命中一个的比例",
    "diagnose.precision": "诊断：逐卡精确率均值（防堆卡）",
    "action.accuracy": "动作：15 条（含 5 条护栏拒绝）全部符合预期",
    "vision.field_accuracy": "素材：6 个字段的 field-level 准确率（mock 回放）",
}


@dataclass(slots=True)
class GateCheck:
    name: str
    value: float
    minimum: float
    description: str

    @property
    def ok(self) -> bool:
        return self.value + 1e-9 >= self.minimum

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "minimum": self.minimum,
            "ok": self.ok,
            "description": self.description,
        }


@dataclass(slots=True)
class GateReport:
    checks: list[GateCheck] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.missing and all(c.ok for c in self.checks)

    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [c.as_dict() for c in self.checks],
            "missing": self.missing,
        }

    def format(self) -> str:
        lines = ["回归门禁"]
        for check in self.checks:
            mark = "PASS" if check.ok else "FAIL"
            lines.append(f"  [{mark}] {check.name:<24} {check.value:.3f} (>= {check.minimum:.2f})  {check.description}")
        for name in self.missing:
            lines.append(f"  [SKIP] {name:<24} 套件未跑，无法判定")
        lines.append(f"  结论：{'通过' if self.passed else '未通过'}")
        return "\n".join(lines)


def _metric_value(results: dict[str, SuiteResult], metric: str) -> float | None:
    suite, _, key = metric.partition(".")
    result = results.get(suite)
    if result is None or result.error or not result.cases:
        return None
    if key == "accuracy":
        return result.accuracy
    meta = result.meta or {}
    raw = meta.get(key)
    return float(raw) if isinstance(raw, (int, float)) else None


def evaluate(results: dict[str, SuiteResult], gates: dict[str, float] | None = None) -> GateReport:
    """按阈值表判定；套件没跑或报错时记进 ``missing``（不静默通过）。"""
    thresholds = dict(GATE_DEFAULTS if gates is None else gates)
    report = GateReport()
    for metric, minimum in thresholds.items():
        value = _metric_value(results, metric)
        if value is None:
            report.missing.append(metric)
            continue
        report.checks.append(
            GateCheck(metric, value, float(minimum), GATE_DESCRIPTIONS.get(metric, ""))
        )
    return report
