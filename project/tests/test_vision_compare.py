"""素材对比与素材证据的离线测试。

全部用 conftest 的测试库与内存 dict，不读真实图片、不调视觉模型。
"""

from __future__ import annotations

from app.core.config import get_settings
from app.vision.compare import _text_verdict, compare, peer_benchmark
from app.vision.material import (
    CN_TO_CODE,
    MATERIAL_METRIC,
    attach_to_cards,
    findings,
    material_score,
    normalize_flags,
)
from app.vision.schema import ISSUE_SPECS


def _db() -> str:
    return get_settings().db_path


def _features(**overrides):
    base = {
        "sku_id": "SKU0001",
        "role": "main",
        "text_blocks": [],
        "text_coverage": 0.0,
        "model_present": True,
        "scene": "室内棚拍",
        "background": "纯色",
        "color_palette": ["#F2E9DD"],
        "size_chart_present": True,
        "watermark": False,
        "collage": False,
        "quality_flags": [],
        "confidence": 1.0,
        "source": "mock",
    }
    base.update(overrides)
    return base


def test_peer_benchmark_fields_on_test_db(db_path):
    db = _db()
    res = peer_benchmark(db, "SKU0001")
    assert res["peer_count"] >= 0
    assert res["scope"] == "同款式中位"
    assert isinstance(res["style"], str) and res["style"]
    for key in (
        "text_coverage_median",
        "size_chart_rate",
        "model_present_rate",
        "watermark_rate",
        "collage_rate",
    ):
        assert 0.0 <= float(res[key]) <= 1.0
    assert res["peer_count"] > 0  # 测试库同款式有其它 SKU


def test_peer_benchmark_unknown_sku_does_not_raise(db_path):
    res = peer_benchmark(_db(), "SKU_NOT_EXIST")
    assert res["peer_count"] == 0
    assert res["text_coverage_median"] == 0.0
    assert res["scope"] == "同款式中位"


def test_peer_benchmark_limit_zero_does_not_raise(db_path):
    res = peer_benchmark(_db(), "SKU0001", limit=0)
    assert res["peer_count"] == 0
    assert res["size_chart_rate"] == 0.0


def test_peer_benchmark_respects_limit(db_path):
    res = peer_benchmark(_db(), "SKU0001", limit=2)
    assert 0 <= res["peer_count"] <= 2


def test_compare_shape(db_path):
    res = compare(_db(), "SKU0001")
    assert res["sku_id"] == "SKU0001"
    assert res["role"] == "main"
    assert res["ours"]["source"]
    assert res["peer"]["scope"] == "同款式中位"
    assert res["gaps"] and res["summary"]
    assert "竞品" not in res["summary"]
    assert res["meta"]["engine"] == "peer-v1"
    assert isinstance(res["meta"]["elapsed_ms"], int)
    assert res["meta"]["degraded"] is False

    fields = {g["field"] for g in res["gaps"]}
    assert {
        "text_coverage",
        "size_chart_present",
        "model_present",
        "watermark",
        "collage",
    } <= fields
    for gap in res["gaps"]:
        assert gap["verdict"] in ("worse", "better", "same")
        assert isinstance(gap["text"], str) and gap["text"]


def test_compare_text_gap_verdicts():
    assert _text_verdict(0.50, 0.30) == "worse"
    assert _text_verdict(0.35, 0.30) == "same"
    assert _text_verdict(0.20, 0.35) == "better"


def test_material_score_all_good():
    assert material_score({"quality_flags": []}) == 1.0


def test_material_score_no_size_chart():
    assert material_score({"quality_flags": ["NO_SIZE_CHART"]}) == 0.75


def test_material_score_all_bad_not_negative():
    codes = list(ISSUE_SPECS)
    assert material_score({"quality_flags": codes}) == 0.0


def test_material_score_normalizes_chinese_flags():
    assert CN_TO_CODE["无尺码表"] == "NO_SIZE_CHART"
    assert normalize_flags(["无尺码表", "NO_SIZE_CHART", "未知flag"]) == ["NO_SIZE_CHART"]
    assert material_score({"quality_flags": ["无尺码表", "无尺码表"]}) == 0.75


def test_findings_codes_and_severity():
    fs = findings({"quality_flags": list(ISSUE_SPECS) + ["未知flag"]})
    assert len(fs) == len(ISSUE_SPECS)
    for item in fs:
        assert item["code"] in ISSUE_SPECS
        assert item["severity"] in ("high", "medium", "low")
        assert item["label"] and item["detail"]
        assert isinstance(item["root_causes"], list)
        assert item["peer_gap"] == ""


def test_findings_peer_gap_from_compare():
    cmp = {
        "gaps": [
            {"field": "text_coverage", "text": "文字占比高于同款式中位 12 个百分点"},
            {"field": "size_chart_present", "text": "主图缺尺码表"},
        ]
    }
    fs = findings(
        {"quality_flags": ["TEXT_HEAVY", "NO_SIZE_CHART"]},
        compare_result=cmp,
    )
    by_code = {f["code"]: f for f in fs}
    assert by_code["TEXT_HEAVY"]["peer_gap"] == "文字占比高于同款式中位 12 个百分点"
    assert by_code["NO_SIZE_CHART"]["peer_gap"] == "主图缺尺码表"
    assert by_code["TEXT_HEAVY"]["severity"] == "high"


def test_attach_r2_card_attached():
    cards = [{"card_id": "c1", "code": "R2", "scope": {"dim": "sku_id", "dim_value": "SKU0001"}}]
    items = {
        "SKU0001": {
            "features": _features(quality_flags=["NO_SIZE_CHART"]),
            "compare": {"summary": "主图最明显的短板是缺尺码表", "gaps": []},
        }
    }
    res = attach_to_cards(cards, items)
    assert res == {"attached": 1, "skipped": 0, "skus": ["SKU0001"]}
    mat = cards[0]["material"]
    assert mat["score"] == 0.75
    assert mat["findings"] and mat["findings"][0]["code"] == "NO_SIZE_CHART"
    assert mat["compare_summary"] == "主图最明显的短板是缺尺码表"


def test_attach_r4_card_attached():
    cards = [{"card_id": "c1", "code": "R4", "scope": {"dim": "sku_id", "dim_value": "SKU0001"}}]
    items = {"SKU0001": {"features": _features(), "compare": None}}
    res = attach_to_cards(cards, items)
    assert res["attached"] == 1
    assert "material" in cards[0]


def test_attach_r5_card_not_attached():
    cards = [{"card_id": "c5", "code": "R5", "scope": {"dim": "sku_id", "dim_value": "SKU0001"}}]
    res = attach_to_cards(cards, {"SKU0001": {"features": _features(), "compare": None}})
    assert res == {"attached": 0, "skipped": 1, "skus": []}
    assert "material" not in cards[0]


def test_attach_missing_sku_skipped():
    cards = [{"card_id": "c1", "code": "R2", "scope": {"dim": "sku_id", "dim_value": "SKU9999"}}]
    res = attach_to_cards(cards, {"SKU0001": {"features": _features(), "compare": None}})
    assert res == {"attached": 0, "skipped": 1, "skus": []}
    assert "material" not in cards[0]


def test_attach_counts_and_ignores_non_sku_scope():
    cards = [
        {"card_id": "c1", "code": "R2", "scope": {"dim": "sku_id", "dim_value": "SKU0001"}},
        {"card_id": "c2", "code": "R4", "scope": {"dim": "sku_id", "dim_value": "SKU0002"}},
        {"card_id": "c3", "code": "R5", "scope": {"dim": "sku_id", "dim_value": "SKU0001"}},
        {"card_id": "c4", "code": "R2", "scope": {"dim": "sku_id", "dim_value": "SKU9999"}},
        {"card_id": "c5", "code": "R2", "scope": {"dim": "channel", "dim_value": "推荐"}},
    ]
    items = {
        "SKU0001": {"features": _features(), "compare": None},
        "SKU0002": {"features": _features(sku_id="SKU0002"), "compare": None},
    }
    res = attach_to_cards(cards, items)
    assert res == {"attached": 2, "skipped": 2, "skus": ["SKU0001", "SKU0002"]}
    assert "material" not in cards[2] and "material" not in cards[3]
    assert "material" not in cards[4]


def test_attach_evidence_fields():
    cards = [{"card_id": "c1", "code": "R2", "scope": {"dim": "sku_id", "dim_value": "SKU0001"}}]
    features = _features(
        text_coverage=0.38,
        quality_flags=["TEXT_HEAVY"],
        watermark=True,
        collage=True,
    )
    items = {"SKU0001": {"features": features, "compare": None}}
    attach_to_cards(cards, items)

    ev = cards[0]["material"]["evidence"]
    assert MATERIAL_METRIC == "material_health"
    assert ev["kind"] == "material"
    assert ev["sku_id"] == "SKU0001"
    assert ev["role"] == "main"
    assert ev["source"] == "mock"
    assert ev["confidence"] == 1.0
    assert "main 图" in ev["text"] and "文字占比 38%" in ev["text"]
    assert "有水印" in ev["text"] and "拼接图" in ev["text"]
    assert ev["issues"] == cards[0]["material"]["findings"]
