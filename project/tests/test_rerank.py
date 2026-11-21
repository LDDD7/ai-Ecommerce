"""三级重排测试（spec §20.4）：规则分各分项 / 关键词覆盖率 / 排序 / 降级 / as_dict。

约束：任何用例都不得真正加载 CrossEncoder 或 torch。cross / llm 档一律把 `model_path`
指向 `.tmp/` 下的**不存在路径**，走「路径校验失败 → 降级 rule」分支，连 worker 线程都不起。
需要改配置时用 `monkeypatch.setattr` 打补丁，不动 `.env`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.retrieval import rerank as rerank_mod
from app.retrieval.hybrid import Hit
from app.retrieval.rerank import RankedItem, keyword_coverage, rerank, rule_score, segments
from app.retrieval.texts import Doc

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"
MISSING_MODEL = TMP / "missing-rerank-model__never_created"

META_KEYS = {
    "mode",
    "requested",
    "n_in",
    "n_out",
    "ce_used",
    "ce_ms",
    "ce_load_ms",
    "dropped",
    "degraded",
    "reason",
    "llm_requested",
    "rule_top",
}


def card(
    card_id: str = "C-R4",
    *,
    root_causes: tuple[str, ...] = (),
    metrics: tuple[str, ...] = (),
    risk: int = 1,
    title: str = "补全尺码表并置顶",
    body: str = "详情页缺少尺码表，退货率与差评率同步上升。",
) -> Doc:
    return Doc(
        owner_table="playbook",
        owner_id=card_id,
        title=title,
        body=body,
        tags=[],
        meta={
            "risk": risk,
            "scope": ["全店"],
            "action": "修复详情页",
            "metrics": list(metrics),
            "root_causes": list(root_causes),
            "window_days": 14,
        },
    )


def hit(doc: Doc, rrf: float, fts_rank: int | None = None, vec_rank: int | None = None) -> Hit:
    return Hit(doc=doc, rrf=rrf, fts_rank=fts_rank, vec_rank=vec_rank)


@pytest.fixture()
def settings(monkeypatch):
    """默认档 + 把模型路径指向不存在的目录，保证 cross 只走降级分支。"""
    current = get_settings()
    monkeypatch.setattr(current, "rerank_mode", "rule")
    monkeypatch.setattr(current, "rerank_model_path", str(MISSING_MODEL))
    monkeypatch.setattr(current, "rerank_top_n", 6)
    monkeypatch.setattr(current, "rerank_timeout_ms", 3500)
    assert not MISSING_MODEL.exists(), "该路径必须不存在，否则用例会真的加载模型"
    return current


# --------------------------------------------------------------------------- #
# 第一级：规则分
# --------------------------------------------------------------------------- #
def test_rule_score_breakdown_matches_components(settings) -> None:
    doc = card(root_causes=("R4", "R10"), metrics=("退款率",), risk=1)
    total, breakdown = rule_score(
        doc,
        root_causes=("R4",),
        metrics=("退款率", "加购率"),
        risk_max=2,
        success_rate=0.8,
        freshness=0.5,
        query="尺码表 退货率",
    )
    assert set(breakdown) == {"root", "metric", "trigger", "success", "risk", "freshness", "total"}
    assert breakdown["root"] == pytest.approx(3.0)
    assert breakdown["metric"] == pytest.approx(2.0)
    assert breakdown["trigger"] == pytest.approx(1.5)  # 「尺码表」「退货率」都命中 → 1.0 * 1.5
    assert breakdown["success"] == pytest.approx(0.4)
    assert breakdown["risk"] == 0.0
    assert breakdown["freshness"] == pytest.approx(0.15)
    parts = sum(breakdown[key] for key in ("root", "metric", "trigger", "success", "risk", "freshness"))
    assert abs(breakdown["total"] - parts) < 1e-9
    assert total == pytest.approx(breakdown["total"])


def test_rule_score_prefers_root_cause_hit(settings) -> None:
    matched = card("C-R4", root_causes=("R4",))
    other = card("C-R2", root_causes=("R2",))
    matched_total, _ = rule_score(matched, root_causes=("R4",))
    other_total, _ = rule_score(other, root_causes=("R4",))
    assert matched_total - other_total == pytest.approx(3.0)


def test_rule_score_adds_metric_bonus(settings) -> None:
    with_metric = card("C-1", metrics=("退款率",))
    without = card("C-2", metrics=("加购率",))
    gained, breakdown = rule_score(with_metric, metrics=("退款率",))
    baseline, _ = rule_score(without, metrics=("退款率",))
    assert breakdown["metric"] == pytest.approx(2.0)
    assert gained - baseline == pytest.approx(2.0)


def test_rule_score_risk_over_limit_is_penalised(settings) -> None:
    risky = card("C-risky", risk=4)
    _, breakdown = rule_score(risky, risk_max=2, success_rate=0.0, freshness=0.0)
    assert breakdown["risk"] == pytest.approx(-1.0)  # -0.5 * (4 - 2)
    assert breakdown["total"] == pytest.approx(-1.0)

    borderline = card("C-safe", risk=2)
    _, safe_breakdown = rule_score(borderline, risk_max=2, success_rate=0.0)
    assert safe_breakdown["risk"] == 0.0


def test_rule_score_success_and_freshness_terms(settings) -> None:
    total, breakdown = rule_score(card("C-only", risk=0), success_rate=1.0, freshness=1.0)
    assert breakdown["success"] == pytest.approx(0.5)
    assert breakdown["freshness"] == pytest.approx(0.3)
    assert total == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# 关键词覆盖率
# --------------------------------------------------------------------------- #
def test_keyword_coverage_chinese_with_punctuation(settings) -> None:
    doc = card(title="详情页尺码表补全", body="退货率下降，差评率下降。")
    assert keyword_coverage("详情页、尺码表，退货率", doc) == pytest.approx(1.0)
    assert keyword_coverage("尺码表 不存在的词", doc) == pytest.approx(0.5)
    assert keyword_coverage("", doc) == 0.0
    assert keyword_coverage(" ，、。 ", doc) == 0.0


def test_keyword_coverage_drops_short_fragments(settings) -> None:
    doc = card(title="详情页尺码表补全", body="退货率下降。")
    assert segments("尺码 表 退货率") == ["尺码", "退货率"]  # 单字「表」被丢弃
    assert keyword_coverage("尺码 表 退货率", doc) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 排序与截断
# --------------------------------------------------------------------------- #
def test_mode_off_sorts_by_rrf_only(settings) -> None:
    items = [
        hit(card("C-low", root_causes=("R4",)), 0.01),
        hit(card("C-high", root_causes=("R2",)), 0.03),
    ]
    out, meta = rerank("退款率 尺码表", items, root_causes=("R4",), mode="off", top_n=5)
    assert [item.doc.owner_id for item in out] == ["C-high", "C-low"]
    assert out[0].score == pytest.approx(0.03)
    assert out[0].rule_score == 0.0 and out[0].breakdown == {}
    assert meta["mode"] == "off" and meta["ce_used"] is False


def test_mode_rule_sorts_by_rule_score_then_rrf(settings) -> None:
    items = [
        hit(card("C-R2", root_causes=("R2",)), 0.05),
        hit(card("C-R4", root_causes=("R4",)), 0.01),
    ]
    out, meta = rerank("退款率", items, root_causes=("R4",), mode="rule")
    assert [item.doc.owner_id for item in out] == ["C-R4", "C-R2"]
    assert out[0].rule_score > out[1].rule_score
    assert out[0].score == pytest.approx(out[0].rule_score)
    assert meta["rule_top"] == ["playbook:C-R4", "playbook:C-R2"]

    tied = [
        hit(card("C-tie-low", root_causes=("R4",)), 0.01),
        hit(card("C-tie-high", root_causes=("R4",)), 0.04),
    ]
    tied_out, _ = rerank("", tied, root_causes=("R4",), mode="rule")
    assert [item.doc.owner_id for item in tied_out] == ["C-tie-high", "C-tie-low"]


def test_top_n_truncates_candidates(settings) -> None:
    items = [hit(card(f"C-{i}"), 0.1 - i * 0.01) for i in range(5)]
    out, meta = rerank("尺码表", items, mode="rule", top_n=2)
    assert len(out) == 2
    assert meta["n_in"] == 5 and meta["n_out"] == 2
    assert len(meta["rule_top"]) == 2


def test_top_n_falls_back_to_settings(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "rerank_top_n", 1)
    items = [hit(card("C-a"), 0.02), hit(card("C-b"), 0.01)]
    out, _ = rerank("尺码表", items, mode="rule")
    assert [item.doc.owner_id for item in out] == ["C-a"]


def test_empty_items_returns_empty_and_meta(settings) -> None:
    for mode in ("off", "rule", "cross"):
        out, meta = rerank("退款率", [], mode=mode)
        assert out == []
        assert meta["n_in"] == 0 and meta["n_out"] == 0
        assert META_KEYS <= set(meta)
    assert rerank_mod.ce_loaded() is False


# --------------------------------------------------------------------------- #
# 降级（绝不加载模型）
# --------------------------------------------------------------------------- #
def test_cross_missing_model_path_degrades_to_rule(settings) -> None:
    items = [
        hit(card("C-R4", root_causes=("R4",)), 0.01),
        hit(card("C-R2", root_causes=("R2",)), 0.05),
    ]
    out, meta = rerank("退款率", items, root_causes=("R4",), mode="cross", top_n=5)
    assert meta["degraded"] is True
    assert meta["mode"] == "rule"
    assert meta["ce_used"] is False
    assert meta["reason"].startswith("ce_unavailable:")
    assert "not found" in meta["reason"]
    assert meta["ce_ms"] == 0.0 and meta["ce_load_ms"] == 0.0
    assert out, "降级后仍要给出规则序结果"
    assert [item.doc.owner_id for item in out] == ["C-R4", "C-R2"]
    assert rerank_mod.ce_loaded() is False


def test_cross_empty_model_path_degrades(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "rerank_model_path", "")
    out, meta = rerank("退款率", [hit(card(), 0.02)], mode="cross")
    assert out and meta["degraded"] is True
    assert meta["reason"] == "ce_unavailable: rerank_model_path is empty"


def test_llm_mode_runs_as_cross_and_flags_request(settings) -> None:
    items = [hit(card("C-R4", root_causes=("R4",)), 0.01), hit(card("C-R2", root_causes=("R2",)), 0.05)]
    out, meta = rerank("退款率", items, root_causes=("R4",), mode="llm")
    assert meta["llm_requested"] is True
    assert meta["requested"] == "llm"
    assert meta["degraded"] is True and meta["mode"] == "rule"
    assert out and out[0].doc.owner_id == "C-R4"


def test_unknown_mode_falls_back_to_rule(settings) -> None:
    out, meta = rerank("退款率", [hit(card(), 0.02)], mode="bogus")
    assert out and meta["mode"] == "rule"
    assert meta["degraded"] is True
    assert meta["reason"] == "unknown_mode: bogus"


def test_meta_keys_present_for_every_mode(settings) -> None:
    items = [hit(card(), 0.02)]
    for mode in ("off", "rule", "cross", "llm"):
        _, meta = rerank("退款率", items, mode=mode)
        assert META_KEYS <= set(meta), (mode, sorted(META_KEYS - set(meta)))
        assert isinstance(meta["n_in"], int) and isinstance(meta["rule_top"], list)


# --------------------------------------------------------------------------- #
# 输出契约
# --------------------------------------------------------------------------- #
def test_as_dict_shape_and_rounding(settings) -> None:
    doc = card(root_causes=("R4",), metrics=("退款率",), risk=1)
    total, breakdown = rule_score(doc, root_causes=("R4",), metrics=("退款率",), query="尺码表")
    item = RankedItem(
        doc=doc,
        rrf=0.0333333,
        fts_rank=1,
        vec_rank=None,
        rule_score=total,
        ce_score=0.123456,
        score=total + 0.5 * 0.123456,
        breakdown={**breakdown, "ce": 0.5 * 0.123456},
    )
    payload = item.as_dict()
    assert set(payload) == {
        "owner_table",
        "owner_id",
        "title",
        "snippet",
        "meta",
        "rrf",
        "fts_rank",
        "vec_rank",
        "rule_score",
        "ce_score",
        "score",
        "breakdown",
    }
    assert payload["owner_table"] == "playbook"
    assert payload["owner_id"] == "C-R4"
    assert payload["title"] == "补全尺码表并置顶"
    assert payload["snippet"].startswith("详情页缺少尺码表")
    assert payload["rrf"] == 0.0333
    assert payload["fts_rank"] == 1
    assert payload["vec_rank"] is None
    assert payload["ce_score"] == 0.1235
    assert payload["rule_score"] == round(total, 4)
    assert payload["breakdown"]["total"] == round(total, 4)
    assert payload["breakdown"]["ce"] == round(0.5 * 0.123456, 4)
    assert payload["meta"]["risk"] == 1

    no_ce = RankedItem(doc=doc, rrf=0.5, rule_score=1.0, ce_score=None, score=1.0).as_dict()
    assert no_ce["ce_score"] is None
