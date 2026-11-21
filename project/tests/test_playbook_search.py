"""策略库检索测试：全部用假 Hit + 假重排。

**绝不触发向量模型**：`hybrid_search` 与 `rerank` 都在模块层被 monkeypatch 掉，
bge-m3 / bge-reranker 一次都不会加载（首次编码约 47s，测试里等不起）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.playbook import ROOT_CAUSES, load_cards
from app.playbook import search as search_mod
from app.playbook.schema import Card
from app.playbook.search import SearchHit, search_for_codes, search_playbook
from app.retrieval.hybrid import Hit
from app.retrieval.texts import Doc

CARDS: dict[str, Card] = {card.card_id: card for card in load_cards()}


def _doc(card: Card) -> Doc:
    """按 app.retrieval.texts.load_playbooks() 的真实形状造 Doc。"""
    return Doc(
        owner_table="playbook",
        owner_id=card.card_id,
        title=card.title,
        body=card.search_text(),
        tags=list(card.root_causes),
        meta={
            "risk": card.risk,
            "scope": list(card.scope),
            "action": card.action_type,
            "metrics": list(card.metrics),
            "root_causes": list(card.root_causes),
            "window_days": card.expected_effect.window_days,
        },
    )


def _hit(card_id: str, rrf: float, fts_rank: int | None = None, vec_rank: int | None = None) -> Hit:
    return Hit(doc=_doc(CARDS[card_id]), rrf=rrf, fts_rank=fts_rank, vec_rank=vec_rank)


@dataclass(slots=True)
class _FakeRanked:
    """模仿 rerank.RankedItem 的字段，验证字段透传。"""

    doc: Doc
    rrf: float
    fts_rank: int | None = None
    vec_rank: int | None = None
    rule_score: float = 0.0
    ce_score: float | None = None
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)


class _Recorder:
    """假 hybrid_search / rerank，记录调用参数并按 rrf 直通排序。"""

    def __init__(self) -> None:
        self.hits: list[Hit] = []
        self.calls: list[dict] = []
        self.rerank_calls: list[dict] = []

    def hybrid(
        self,
        db_path,
        query,
        docs=None,
        owner_table=None,
        limit=12,
        with_vec=None,
    ):
        self.calls.append(
            {
                "db_path": db_path,
                "query": query,
                "docs": docs,
                "owner_table": owner_table,
                "limit": limit,
                "with_vec": with_vec,
            }
        )
        return list(self.hits), {
            "query": query,
            "fts": sum(1 for hit in self.hits if hit.fts_rank),
            "vec": sum(1 for hit in self.hits if hit.vec_rank),
            "degraded": True,
        }

    def rerank(
        self,
        query,
        items,
        *,
        root_causes=(),
        metrics=(),
        risk_max=2,
        mode=None,
        top_n=None,
        timeout_ms=None,
    ):
        self.rerank_calls.append(
            {
                "query": query,
                "root_causes": tuple(root_causes),
                "metrics": tuple(metrics),
                "risk_max": risk_max,
                "mode": mode,
                "top_n": top_n,
            }
        )
        roots = set(root_causes)
        ranked = [
            _FakeRanked(
                doc=item.doc,
                rrf=item.rrf,
                fts_rank=item.fts_rank,
                vec_rank=item.vec_rank,
                rule_score=3.0 if roots & set(item.doc.meta["root_causes"]) else 0.0,
                ce_score=None,
                score=item.rrf,
            )
            for item in items
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        if top_n is not None:  # 对齐真实 rerank：按 top_n 截断
            ranked = ranked[: int(top_n)]
        return ranked, {"mode": mode or "rule", "degraded": False}


@pytest.fixture
def fake(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(search_mod, "hybrid_search", recorder.hybrid)
    monkeypatch.setattr(search_mod, "rerank", recorder.rerank)
    return recorder


def _twelve() -> list[str]:
    return sorted(CARDS)[:12]


# ---- search_playbook ----


def test_score_desc_and_limit(fake):
    order = _twelve()
    fake.hits = [
        _hit(card_id, rrf=0.02 - i * 0.001, fts_rank=i + 1) for i, card_id in enumerate(order)
    ]

    hits, meta = search_playbook("unused.db", "退款率升高，尺码相关", limit=5)

    assert meta["recall"] == 12
    assert meta["returned"] == 5
    assert len(hits) == 5
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert [hit.card.card_id for hit in hits] == order[:5]


def test_hybrid_called_with_playbook_table_and_recall_limit(fake):
    fake.hits = [_hit("PB-R4-01", rrf=0.5)]

    search_playbook("unused.db", "尺码表缺失", limit=5)

    assert fake.calls, "search_playbook 必须调用 hybrid_search"
    call = fake.calls[0]
    assert call["owner_table"] == "playbook"
    assert call["limit"] == 12
    assert call["query"] == "尺码表缺失"


def test_rerank_receives_filters_and_top_n(fake):
    fake.hits = [_hit("PB-R4-01", rrf=0.5)]

    search_playbook("unused.db", "尺码", root_causes=["R4"], metrics=["refund_rate"], limit=3)

    call = fake.rerank_calls[0]
    assert call["query"] == "尺码"
    assert call["root_causes"] == ("R4",)
    assert call["metrics"] == ("refund_rate",)
    assert call["top_n"] == 3
    # 风险不在重排里过滤/惩罚，否则 filtered_risk 统计不到
    assert call["risk_max"] == 2


def test_meta_contract_keys(fake):
    fake.hits = [_hit("PB-R4-01", rrf=0.5)]

    _, meta = search_playbook("unused.db", "尺码", root_causes=["R4"], metrics=["refund_rate"])

    required = {
        "query",
        "root_causes",
        "metrics",
        "risk_max",
        "recall",
        "returned",
        "filtered_risk",
        "fts",
        "vec",
        "mode",
        "degraded",
        "reason",
        "note",
    }
    assert required <= set(meta)
    assert meta["root_causes"] == ["R4"]
    assert meta["metrics"] == ["refund_rate"]
    assert meta["risk_max"] == 2
    assert meta["clamped"] is False


def test_risk_max_zero_keeps_only_safe_cards(fake):
    order = _twelve()
    fake.hits = [_hit(card_id, rrf=1.0 - i * 0.01) for i, card_id in enumerate(order)]

    # limit=12 让 top_n=12，重排不会提前截断，filtered_risk 才应是全量计数
    hits, meta = search_playbook("unused.db", "退款率升高", risk_max=0, limit=12)

    assert hits, "12 条召回里至少有 PB-R3-01 是 risk 0，不应被全过滤"
    assert all(hit.risk == 0 for hit in hits)
    expected_filtered = sum(1 for card_id in order if CARDS[card_id].risk > 0)
    assert meta["filtered_risk"] == expected_filtered
    assert meta["returned"] == len(hits)


def test_reasons_include_root_metric_and_action(fake):
    fake.hits = [_hit("PB-R4-01", rrf=0.5, fts_rank=1, vec_rank=2)]

    hits, _ = search_playbook(
        "unused.db", "退款偏多，疑似尺码", root_causes=["R4"], metrics=["refund_rate"]
    )

    reasons = hits[0].reasons
    assert 1 <= len(reasons) <= 4
    assert any("尺码与描述不符" in reason for reason in reasons)
    assert any(f"命中根因 R4 {ROOT_CAUSES['R4']}" == reason for reason in reasons)
    assert any("改善 refund_rate" in reason for reason in reasons)
    assert any("换素材" in reason for reason in reasons)
    assert any("fts#1" in reason and "vec#2" in reason for reason in reasons)


def test_reasons_capped_at_four(fake):
    fake.hits = [_hit("PB-R4-01", rrf=0.5)]

    hits, _ = search_playbook(
        "unused.db",
        "退款率高",
        root_causes=["R4"],
        metrics=["refund_rate", "return_rate", "neg_review_rate"],
    )

    reasons = hits[0].reasons
    assert len(reasons) == 4
    assert len(set(reasons)) == 4, f"理由不应重复：{reasons}"


def test_degraded_to_rrf_when_rerank_missing(fake, monkeypatch):
    monkeypatch.setattr(search_mod, "rerank", None)
    # 故意乱序：降级后必须自己按 rrf 重排
    fake.hits = [_hit("PB-R4-01", rrf=0.01), _hit("PB-R4-02", rrf=0.02)]

    hits, meta = search_playbook("unused.db", "尺码不符", limit=5)

    assert [hit.card.card_id for hit in hits] == ["PB-R4-02", "PB-R4-01"]
    assert meta["degraded"] is True
    assert meta["reason"] == "rerank_unavailable"
    assert meta["returned"] == 2


def test_rerank_self_degraded_is_propagated(fake, monkeypatch):
    """cross 档 CE 不可用时 rerank 会静默降级回 rule，meta 必须透传。"""

    def rerank(
        query,
        items,
        *,
        root_causes=(),
        metrics=(),
        risk_max=2,
        mode=None,
        top_n=None,
        timeout_ms=None,
    ):
        ranked = [
            _FakeRanked(doc=item.doc, rrf=item.rrf, rule_score=1.0, score=item.rrf)
            for item in items
        ]
        return ranked, {
            "mode": "rule",
            "requested": "cross",
            "degraded": True,
            "reason": "ce_unavailable: model path is empty",
        }

    monkeypatch.setattr(search_mod, "rerank", rerank)
    fake.hits = [_hit("PB-R4-01", rrf=0.5)]

    hits, meta = search_playbook("unused.db", "尺码", mode="cross")

    assert hits, "降级也要出结果"
    assert meta["degraded"] is True
    assert meta["mode"] == "rule", "实际跑的是 rule 档，要如实反映"
    assert "ce_unavailable" in meta["reason"]


def test_empty_recall_returns_note(fake):
    fake.hits = []

    hits, meta = search_playbook("unused.db", "完全不相关的查询", limit=5)

    assert hits == []
    assert meta["returned"] == 0
    assert meta["recall"] == 0
    assert "建议人工介入" in meta["note"]


def test_policy_docs_are_filtered_out(fake):
    policy = Doc("policy_docs", "POL-001", "七天无理由规则", "平台售后规则正文", [], {})
    fake.hits = [Hit(doc=policy, rrf=0.9), _hit("PB-R4-01", rrf=0.5)]

    hits, meta = search_playbook("unused.db", "退款", limit=5)

    assert [hit.card.card_id for hit in hits] == ["PB-R4-01"]
    assert meta["recall"] == 1, "policy 文档不能计入策略卡召回"


def test_order_follows_rerank_score_and_keeps_breakdown(fake, monkeypatch):
    def rerank(
        query,
        items,
        *,
        root_causes=(),
        metrics=(),
        risk_max=2,
        mode=None,
        top_n=None,
        timeout_ms=None,
    ):
        ranked = [
            _FakeRanked(
                doc=item.doc,
                rrf=item.rrf,
                fts_rank=item.fts_rank,
                rule_score=1.5 if item.doc.owner_id == "PB-R3-01" else 0.0,
                ce_score=0.91 if item.doc.owner_id == "PB-R3-01" else 0.05,
                score=2.0 if item.doc.owner_id == "PB-R3-01" else 0.1,
                breakdown={"reason": "test"},
            )
            for item in items
        ]
        return ranked, {"mode": "cross", "degraded": False}

    monkeypatch.setattr(search_mod, "rerank", rerank)
    fake.hits = [_hit("PB-R4-01", rrf=0.9), _hit("PB-R3-01", rrf=0.1)]

    hits, meta = search_playbook("unused.db", "断货还是尺码")

    assert [hit.card.card_id for hit in hits] == ["PB-R3-01", "PB-R4-01"]
    assert hits[0].ce_score == 0.91
    assert hits[0].rule_score == 1.5
    assert meta["mode"] == "cross"
    assert meta["degraded"] is False


def test_limit_is_clamped(fake):
    fake.hits = [_hit(card_id, rrf=1.0 - i * 0.01) for i, card_id in enumerate(_twelve())]

    hits, meta = search_playbook("unused.db", "退款", limit=999)
    assert len(hits) <= 20
    assert meta["clamped"] is True

    hits_low, meta_low = search_playbook("unused.db", "退款", limit=0)
    assert len(hits_low) == 1
    assert meta_low["clamped"] is True


def test_as_dict_shape(fake):
    fake.hits = [_hit("PB-R4-01", rrf=0.5, fts_rank=1)]

    hits, _ = search_playbook("unused.db", "尺码", root_causes=["R4"])
    assert isinstance(hits[0], SearchHit)
    data = hits[0].as_dict()

    assert set(data) == {
        "card_id",
        "title",
        "risk",
        "action_type",
        "scope",
        "root_causes",
        "metrics",
        "score",
        "rule_score",
        "ce_score",
        "rrf",
        "reasons",
        "expected_effect",
        "success_metric",
        "preconditions",
        "steps",
    }
    assert data["expected_effect"] == {
        "metric": "refund_rate",
        "direction": "down",
        "range": [0.15, 0.35],
        "window_days": 14,
    }
    assert isinstance(data["expected_effect"]["range"], list)
    assert "search_text" not in data
    assert data["steps"], "步骤要带上，执行层直接用"
    assert data["card_id"] == "PB-R4-01"


# ---- search_for_codes ----


def test_search_for_codes_r4():
    for hits, meta in (
        search_for_codes(None, "R4"),
        search_for_codes("R4"),  # 单参兼容写法
    ):
        assert [hit.card.card_id for hit in hits] == ["PB-R4-01", "PB-R4-02"]
        assert meta["engine"] == "by_root"
        assert meta["codes"] == ["R4"]
        assert meta["returned"] == 2
        assert meta["filtered_risk"] == 0
        assert meta["unknown_codes"] == []
        for hit in hits:
            assert any("尺码与描述不符" in reason for reason in hit.reasons)


def test_search_for_codes_unknown_code():
    hits, meta = search_for_codes(None, ["R99"])

    assert hits == []
    assert meta["unknown_codes"] == ["R99"]
    assert meta["returned"] == 0
    assert "建议人工介入" in meta["note"]


def test_search_for_codes_risk_filter_and_limit():
    hits, meta = search_for_codes(None, ["R4"], risk_max=0)
    assert hits == []
    assert meta["filtered_risk"] == 2

    limited, meta_limited = search_for_codes(None, ["R4"], limit=1)
    assert len(limited) == 1
    assert limited[0].card.card_id == "PB-R4-01"
    assert meta_limited["returned"] == 1


def test_search_for_codes_multi_root_scores_hit_count():
    # PB-R7-04 同时覆盖 R7 与 R3，应排在其他单根因卡之前
    hits, meta = search_for_codes(None, ["R7", "R3"])

    assert hits[0].card.card_id == "PB-R7-04"
    assert hits[0].score == 2.0
    # 其余都是命中 1 个码：先 risk 升序，再 card_id
    rest = [(hit.risk, hit.card.card_id) for hit in hits[1:]]
    assert rest == sorted(rest), f"单根因卡应按 risk→card_id 排序：{rest}"
    assert all(hit.score == 1.0 for hit in hits[1:])
    assert meta["returned"] == len(hits)
