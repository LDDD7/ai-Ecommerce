"""素材评测套件（T7-C）。

10 条用例全部来自 ``tools.vision_eval.load_ground_truth(db_path, cases=10, seed=7)``
的分层真值。``mode="mock"`` 直接回放 ``image_feature.features_json``，不联网、
不加载模型，因此本套件在 CI 里是确定性的。

判分复用 ``tools.vision_eval.score_case``：

- 6 个字段全对才算整条通过（``ok``）；
- ``score = fields_ok / total_fields``；
- ``detail`` 带字段级明细、错误文案与 ``features.issues()``（问题码 → 根因/动作）。

缺库、缺 ``image_feature`` 表、真值不足 10 条时，``run()`` 返回带 ``error`` 的
``SuiteResult``，不向调用方抛异常。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.eval.types import Case, CaseResult, SuiteResult
from app.vision.inspector import inspect
from tools.vision_eval import load_ground_truth, score_case

SUITE = "vision"
CASES = 10
SEED = 7


def cases(db_path: str | Path) -> list[Case]:
    """恰好 10 条，``V01``–``V10``，SKU 不重复。"""
    ground_truth = load_ground_truth(db_path, cases=CASES, seed=SEED)
    if len(ground_truth) != CASES:
        raise ValueError(f"素材真值应有 {CASES} 条，实际 {len(ground_truth)} 条")
    sku_ids = [str(gt["sku_id"]) for gt in ground_truth]
    if len(set(sku_ids)) != len(sku_ids):
        raise ValueError("素材真值存在重复 SKU，无法生成 10 条独立用例")
    return [
        Case(
            case_id=f"V{index:02d}",
            suite=SUITE,
            payload={
                "label": f"{gt['sku_id']} · 素材属性",
                "sku_id": str(gt["sku_id"]),
                "gt": gt,
            },
            note="mock 回放 image_feature 真值，6 字段全对才算通过",
        )
        for index, gt in enumerate(ground_truth, start=1)
    ]


def _run_case(db_path: str | Path, case: Case) -> CaseResult:
    sku_id = str(case.payload["sku_id"])
    gt = case.payload["gt"]
    try:
        features, meta = inspect(db_path, sku_id, "main", mode="mock")
        scored = score_case(gt, features)
        fields_ok = int(scored["fields_ok"])
        total_fields = int(scored["total_fields"])
        ok = fields_ok == total_fields
        detail: dict[str, Any] = {
            "sku_id": sku_id,
            "fields_ok": fields_ok,
            "total_fields": total_fields,
            "field_detail": scored["field_detail"],
            "errors": scored["errors"],
            "issues": [issue.model_dump() for issue in features.issues()],
            "flags_gt": scored["flags_gt"],
            "flags_got": scored["flags_got"],
            "source": scored["source"],
            "mode": meta.get("mode"),
            "degraded": bool(meta.get("degraded")),
        }
        return CaseResult(case.case_id, ok, fields_ok / total_fields if total_fields else 0.0, detail)
    except Exception as exc:  # noqa: BLE001 - 单条素材缺失记失败，不中断其余 9 条
        return CaseResult(
            case.case_id,
            False,
            0.0,
            {"sku_id": sku_id, "error": f"{type(exc).__name__}: {exc}"},
        )


def _meta(results: list[CaseResult], elapsed_ms: float) -> dict[str, Any]:
    fields_ok = sum(int(r.detail.get("fields_ok", 0)) for r in results)
    fields_total = sum(int(r.detail.get("total_fields", 0)) for r in results)
    passed = sum(1 for r in results if r.ok)
    return {
        "field_accuracy": round(fields_ok / fields_total, 4) if fields_total else 0.0,
        "case_accuracy": round(passed / len(results), 4) if results else 0.0,
        "fields_ok": fields_ok,
        "fields_total": fields_total,
        "cases": len(results),
        "avg_ms": round(elapsed_ms / len(results), 3) if results else 0.0,
        "mode": "mock",
    }


def run(db_path: str | Path, *, limit: int | None = None) -> SuiteResult:
    started = time.perf_counter()
    try:
        all_cases = cases(db_path)
    except Exception as exc:  # noqa: BLE001 - 缺库/缺表/真值不足统一转成 error
        elapsed = (time.perf_counter() - started) * 1000.0
        return SuiteResult(
            SUITE,
            [],
            elapsed_ms=elapsed,
            meta={
                "field_accuracy": 0.0,
                "case_accuracy": 0.0,
                "fields_ok": 0,
                "fields_total": 0,
                "cases": 0,
                "avg_ms": 0.0,
                "mode": "mock",
            },
            error=f"无法加载素材真值：{type(exc).__name__}: {exc}",
        )

    selected = all_cases[: max(0, int(limit))] if limit is not None else all_cases
    results = [_run_case(db_path, case) for case in selected]
    elapsed = (time.perf_counter() - started) * 1000.0
    return SuiteResult(
        SUITE,
        results,
        elapsed_ms=elapsed,
        meta=_meta(results, elapsed),
        error=None,
    )
