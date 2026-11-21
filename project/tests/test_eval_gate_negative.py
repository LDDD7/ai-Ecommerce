"""门禁的反向用例：跑出低分时必须判红，否则「门禁」只是装饰。

这里不跑真实引擎，直接造 `SuiteResult` 喂给 `gate.evaluate`：
- 诊断三项低于阈值 → 不通过，且报告里能看到是哪一项；
- 某个套件缺失 → 记为 missing，不静默通过；
- 套件报错（例如数据库坏了）→ 也不通过。
"""

from __future__ import annotations

from app.eval.gate import evaluate
from app.eval.types import Case, CaseResult, SuiteResult


def _suite(name: str, total: int, passed: int, **meta) -> SuiteResult:
    cases = [
        CaseResult(case_id=f"{name}-{i}", ok=i < passed, score=1.0 if i < passed else 0.0)
        for i in range(total)
    ]
    return SuiteResult(suite=name, cases=cases, meta=meta)


def test_诊断低分判红():
    diag = _suite("diagnose", 25, 5, top1_rate=0.20, top3_rate=0.24, precision=0.18)
    report = evaluate(
        {
            "query": _suite("query", 20, 20, **{"accuracy": 1.0}),
            "diagnose": diag,
            "action": _suite("action", 15, 15, **{"accuracy": 1.0}),
            "vision": _suite("vision", 10, 10, field_accuracy=1.0),
        }
    )
    assert report.passed is False
    failed = [c for c in report.checks if not c.ok]
    assert {c.name for c in failed} == {"diagnose.top1_rate", "diagnose.top3_rate", "diagnose.precision"}
    text = report.format()
    assert "[FAIL]" in text


def test_缺套件不静默通过():
    report = evaluate({"query": _suite("query", 20, 20, **{"accuracy": 1.0})})
    assert report.passed is False
    assert "diagnose.top1_rate" in report.missing
    assert "action.accuracy" in report.missing
    assert "[SKIP]" in report.format()


def test_套件报错不算通过():
    broken = SuiteResult(suite="query", cases=[], error="sqlite3.OperationalError: no such table")
    report = evaluate(
        {
            "query": broken,
            "diagnose": _suite("diagnose", 25, 25, top1_rate=1.0, top3_rate=1.0, precision=1.0),
            "action": _suite("action", 15, 15, **{"accuracy": 1.0}),
            "vision": _suite("vision", 10, 10, field_accuracy=1.0),
        }
    )
    assert report.passed is False
    assert "query.accuracy" in report.missing


def test_阈值可覆盖且边界算通过():
    diag = _suite("diagnose", 25, 11, top1_rate=0.44, top3_rate=0.52, precision=0.3867)
    base = {
        "query": _suite("query", 20, 20, **{"accuracy": 1.0}),
        "diagnose": diag,
        "action": _suite("action", 15, 15, **{"accuracy": 1.0}),
        "vision": _suite("vision", 10, 10, field_accuracy=1.0),
    }
    # 默认阈值下应当通过（0.44 >= 0.40 等）
    assert evaluate(base).passed is True
    # 把 top1 抬到 0.45 就该红
    strict = evaluate(base, gates={"diagnose.top1_rate": 0.45})
    assert strict.passed is False
