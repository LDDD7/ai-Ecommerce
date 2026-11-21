"""评测套件的公共数据结构（T7）。

三层：

- ``Case``       一条用例（payload 由各套件自己解释）
- ``CaseResult`` 一条用例的结果（ok / score / detail）
- ``SuiteResult`` 一个套件的结果（含耗时与 meta）

约定：``score`` 一律 0~1，``ok`` 是「是否达到该用例自身的通过线」，
聚合口径（accuracy / precision / recall）由 ``app.eval.metrics`` 计算，
门禁阈值放在 ``app.eval.gate``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Case:
    case_id: str
    suite: str
    payload: dict[str, Any]
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "suite": self.suite, "payload": self.payload, "note": self.note}


@dataclass(slots=True)
class CaseResult:
    case_id: str
    ok: bool
    score: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "ok": self.ok, "score": round(float(self.score), 4), "detail": self.detail}


@dataclass(slots=True)
class SuiteResult:
    suite: str
    cases: list[CaseResult]
    elapsed_ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.ok)

    @property
    def accuracy(self) -> float:
        return 0.0 if not self.cases else self.passed / len(self.cases)

    @property
    def mean_score(self) -> float:
        return 0.0 if not self.cases else sum(c.score for c in self.cases) / len(self.cases)

    def failures(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "total": self.total,
            "passed": self.passed,
            "accuracy": round(self.accuracy, 4),
            "mean_score": round(self.mean_score, 4),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "meta": self.meta,
            "error": self.error,
            "cases": [c.as_dict() for c in self.cases],
        }
