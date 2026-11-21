"""评测基础设施单测：计分工具 + 门禁判定 + 跑批汇总。

这些都是纯函数，不碰数据库，所以不需要夹具。
"""

from __future__ import annotations

import pytest

from app.eval import metrics as M
from app.eval.gate import GATE_DEFAULTS, GateCheck, evaluate
from app.eval.runner import format_table, summarize
from app.eval.types import CaseResult, SuiteResult


class TestNumeric:
    def test_close_within_relative_tolerance(self):
        assert M.numeric_close(123456.789, 123456.789, rel=1e-9)

    def test_close_within_absolute_tolerance(self):
        assert M.numeric_close(0.0200004, 0.02, rel=0, abs_=1e-5)
        assert not M.numeric_close(0.021, 0.02, rel=0, abs_=1e-5)

    def test_non_numeric_is_false(self):
        assert not M.numeric_close("abc", 1.0)
        assert not M.numeric_close(None, 1.0)


class TestSubsetMatch:
    def test_only_checks_expected_keys(self):
        actual = {"gmv": 100.0, "aov": 50.0, "extra": 1}
        assert M.subset_match(actual, {"gmv": 100.0})

    def test_nested_dict(self):
        actual = {"time": {"start": "2026-11-01", "end": "2026-11-07"}}
        assert M.subset_match(actual, {"time": {"start": "2026-11-01"}})

    def test_missing_key_fails(self):
        assert not M.subset_match({"gmv": 1.0}, {"aov": 1.0})

    def test_list_must_match_exactly(self):
        assert M.subset_match({"dims": ["style"]}, {"dims": ["style"]})
        assert not M.subset_match({"dims": ["style"]}, {"dims": ["style", "dt"]})


class TestRanking:
    def test_precision_recall(self):
        p, r = M.precision_recall(["R3", "R7", "R7"], ["R3", "R4"])
        assert p == pytest.approx(0.5)
        assert r == pytest.approx(0.5)

    def test_empty_side_is_zero(self):
        assert M.precision_recall([], ["R3"]) == (0.0, 0.0)
        assert M.precision_recall(["R3"], []) == (0.0, 0.0)

    def test_topk_hit_respects_k(self):
        assert M.topk_hit(["R7", "R3"], ["R3"], k=2)
        assert not M.topk_hit(["R7", "R3"], ["R3"], k=1)

    def test_confusion_counts(self):
        got = M.confusion(["R3", "R5"], ["R3", "R4"])
        assert got == {"hit": 1, "extra": 1, "missed": 1}


class TestScores:
    def test_range_score_plateau_and_decay(self):
        assert M.range_score(0.2, 0.1, 0.3) == 1.0
        assert M.range_score(0.0, 0.1, 0.3) == pytest.approx(0.5)
        assert M.range_score(-1.0, 0.1, 0.3) == 0.0

    def test_weighted_score_normalizes(self):
        got = M.weighted_score({"a": 1.0}, {"a": 1.0, "b": 1.0})
        assert got == pytest.approx(0.5)


def _suite(name: str, oks: list[bool], meta: dict | None = None, error: str | None = None) -> SuiteResult:
    cases = [CaseResult(f"{name}-{i}", ok, 1.0 if ok else 0.0) for i, ok in enumerate(oks)]
    return SuiteResult(name, cases, elapsed_ms=12.0, meta=meta or {}, error=error)


class TestGate:
    def test_all_above_threshold_passes(self):
        results = {
            "query": _suite("query", [True] * 20),
            "diagnose": _suite("diagnose", [True] * 8 + [False] * 2, {"top1_rate": 0.8, "top3_rate": 0.9, "precision": 0.75}),
            "action": _suite("action", [True] * 15),
            "vision": _suite("vision", [True] * 10, {"field_accuracy": 1.0}),
        }
        report = evaluate(results)
        assert report.passed, report.as_dict()

    def test_below_threshold_fails(self):
        results = {
            "query": _suite("query", [True] * 19 + [False]),
            # 阈值取自 GATE_DEFAULTS（0.40 / 0.48 / 0.35），这里三项都压到线下
            "diagnose": _suite("diagnose", [True] * 6, {"top1_rate": 0.2, "top3_rate": 0.3, "precision": 0.2}),
            "action": _suite("action", [True] * 15),
            "vision": _suite("vision", [True] * 10, {"field_accuracy": 0.9}),
        }
        report = evaluate(results)
        assert not report.passed
        names = {c.name for c in report.failures()}
        assert "query.accuracy" in names and "diagnose.top3_rate" in names

    def test_missing_suite_is_not_a_pass(self):
        """套件没跑 / 报错时必须显式 SKIP，不能默认通过。"""
        results = {"query": _suite("query", [True] * 20, error="boom")}
        report = evaluate(results)
        assert not report.passed
        assert "action.accuracy" in report.missing

    def test_custom_thresholds(self):
        results = {"query": _suite("query", [True] * 10)}
        report = evaluate(results, {"query.accuracy": 0.5})
        assert report.passed
        assert report.checks[0].minimum == 0.5

    def test_defaults_cover_all_suites(self):
        assert set(GATE_DEFAULTS) == {
            "query.accuracy",
            "diagnose.top1_rate",
            "diagnose.top3_rate",
            "diagnose.precision",
            "action.accuracy",
            "vision.field_accuracy",
        }

    def test_gate_check_boundary_is_inclusive(self):
        assert GateCheck("x", 0.5, 0.5, "").ok


class TestRunnerBits:
    def test_summarize_keeps_metrics(self):
        rows = summarize(
            {
                "query": _suite("query", [True] * 4),
                "diagnose": _suite("diagnose", [True] * 2, {"top3_rate": 1.0}),
            }
        )
        assert rows[0]["total"] == 4 and rows[0]["accuracy"] == 1.0
        assert rows[1]["meta"] == {"top3_rate": 1.0}

    def test_format_table_has_header_and_rows(self):
        text = format_table(summarize({"query": _suite("query", [True, False])}))
        assert "套件" in text and "query" in text
