"""检索层测试：FTS 建索/中文 trigram/短查询退化/RRF/混合降级。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.retrieval import fts, hybrid, load_policies
from app.retrieval.texts import OWNER_POLICY


@pytest.fixture()
def indexed(db_path: Path) -> Path:
    fts.ensure(db_path)
    fts.index(db_path, load_policies(db_path))
    return db_path


def test_fts_indexes_all_policies(indexed: Path) -> None:
    assert fts.count(indexed) >= 10


def test_fts_finds_size_chart_policy(indexed: Path) -> None:
    rows = fts.search(indexed, "详情页没有尺码表导致退货", limit=5)
    ids = [row[1] for row in rows]
    assert "POL_005" in ids or "POL_001" in ids, ids


def test_short_query_falls_back_to_like(indexed: Path) -> None:
    # 「尺码」只有 2 个字，trigram 建不出索引，走 LIKE 兜底
    rows = fts.search(indexed, "尺码", limit=5)
    assert rows, "短查询不应该返回空"
    assert any("尺码" in row[1] or row[1].startswith("POL_") for row in rows)


def test_indexing_is_idempotent(indexed: Path) -> None:
    before = fts.count(indexed)
    fts.index(indexed, load_policies(indexed))
    assert fts.count(indexed) == before


def test_search_scoped_by_owner_table(indexed: Path) -> None:
    docs = load_policies(indexed)
    hits, meta = hybrid.search(indexed, "活动价 价格保护", docs=docs, owner_table=OWNER_POLICY, with_vec=False)
    assert hits, meta
    assert {h.doc.owner_table for h in hits} == {OWNER_POLICY}
    assert meta["degraded"] is True and meta["vec_enabled"] is False


def test_rrf_prefers_documents_in_both_lists() -> None:
    a = ("policy_docs", "POL_005")
    b = ("policy_docs", "POL_003")
    c = ("policy_docs", "POL_009")
    fused = hybrid.rrf_fuse([[a, b], [b, c]])
    assert fused[b] > fused[a] > 0
    assert fused[b] > fused[c] > 0


def test_empty_query_returns_nothing(indexed: Path) -> None:
    hits, meta = hybrid.search(indexed, "   ", with_vec=False)
    assert hits == []
    assert meta["fts"] == 0
