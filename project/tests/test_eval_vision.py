"""``app.eval.suites.vision_suite`` 的离线测试。

素材套件只走 ``mode="mock"``：读 conftest 小库的 ``image_feature`` 真值，不联网、
不加载模型。缺库 / 缺表分支用 ``.tmp`` 下的空库与不存在的路径覆盖。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from app.eval.suites.vision_suite import SUITE, cases, run
from app.vision.schema import derive_flags

_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "test_eval_vision"


def _workdir() -> Path:
    path = _TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_cases_have_10_unique_ids_and_skus(db_path: Path) -> None:
    items = cases(db_path)

    assert len(items) == 10
    assert [c.case_id for c in items] == [f"V{i:02d}" for i in range(1, 11)]
    assert len({c.case_id for c in items}) == 10
    sku_ids = [str(c.payload["sku_id"]) for c in items]
    assert len(set(sku_ids)) == 10
    for case in items:
        assert case.suite == SUITE
        assert case.payload["gt"]["sku_id"] == case.payload["sku_id"]
        assert "label" in case.payload


def test_run_limit_2_smoke(db_path: Path) -> None:
    result = run(db_path, limit=2)

    assert result.suite == SUITE
    assert result.total == 2
    assert result.error is None
    assert result.meta["mode"] == "mock"
    assert all("fields_ok" in case.detail for case in result.cases)
    assert all(case.detail["fields_ok"] == 6 for case in result.cases)
    assert all(case.ok for case in result.cases)
    assert result.elapsed_ms >= 0


def test_ground_truth_flags_are_consistent_with_derive(db_path: Path) -> None:
    items = cases(db_path)[:2]

    for case in items:
        gt = case.payload["gt"]
        derived = set(
            derive_flags(
                text_coverage=gt["text_coverage"],
                model_present=gt["model_present"],
                scene=gt["scene"],
                background=gt["background"],
                size_chart_present=gt["size_chart_present"],
                watermark=gt["watermark"],
                collage=gt["collage"],
            )
        )
        labelled = set(gt["quality_flags"]) - {"CLEAN"}
        # 真值里每个标签都必须能被属性推出来；允许生成器漏标 NO_MODEL（历史口径）。
        assert labelled <= derived
        assert derived - labelled <= {"NO_MODEL"}

        # 抽样核对布尔标签与属性不矛盾。
        assert gt["watermark"] == ("WATERMARK" in labelled)
        assert gt["collage"] == ("COLLAGE" in labelled)
        assert gt["size_chart_present"] == ("NO_SIZE_CHART" not in labelled)


def test_run_missing_database_returns_error() -> None:
    missing = _workdir() / "does-not-exist" / "store.db"

    result = run(missing)

    assert result.suite == SUITE
    assert result.total == 0
    assert result.error
    assert "无法加载素材真值" in result.error


def test_run_database_without_image_feature_table_returns_error() -> None:
    db = _workdir() / "empty.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE TABLE placeholder (id INTEGER)")
        conn.commit()

    result = run(db)

    assert result.suite == SUITE
    assert result.total == 0
    assert result.error
    assert "image_feature" in result.error


def test_full_run_reports_perfect_mock_accuracy(db_path: Path) -> None:
    result = run(db_path)

    assert result.total == 10
    assert result.error is None
    assert result.passed == 10
    assert result.meta["mode"] == "mock"
    assert result.meta["fields_total"] == 60
    assert result.meta["field_accuracy"] == 1.0
    assert result.meta["case_accuracy"] == 1.0
    for case in result.cases:
        assert case.detail["fields_ok"] == case.detail["total_fields"] == 6
        assert isinstance(case.detail["issues"], list)
        assert case.detail["errors"] == []
