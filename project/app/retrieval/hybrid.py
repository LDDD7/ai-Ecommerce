"""混合召回：FTS5(trigram) + bge-m3 向量，用 RRF 融合（spec §20.3）。

RRF（Reciprocal Rank Fusion）：`score = Σ 1/(k + rank)`，两个榜单的**名次**融合，
不需要把 bm25 分数和余弦分数强行拉到同一量纲——这是它在小语料上最实用的地方。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.retrieval import embed, fts
from app.retrieval.texts import Doc, doc_map

RRF_K = 60
CANDIDATES = 12


@dataclass(slots=True)
class Hit:
    doc: Doc
    rrf: float
    fts_rank: int | None = None
    vec_rank: int | None = None
    fts_score: float | None = None
    vec_score: float | None = None
    score_breakdown: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            **self.doc.as_dict(),
            "rrf": round(self.rrf, 6),
            "fts_rank": self.fts_rank,
            "vec_rank": self.vec_rank,
            "fts_score": None if self.fts_score is None else round(self.fts_score, 6),
            "vec_score": None if self.vec_score is None else round(self.vec_score, 6),
        }


def rrf_fuse(rankings: list[list[tuple[str, str]]], k: int = RRF_K) -> dict[tuple[str, str], float]:
    """rankings 是若干「按相关度排好序的 key 列表」，返回 key → RRF 分。"""
    scores: dict[tuple[str, str], float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


def search(
    db_path: str | Path,
    query: str,
    docs: list[Doc] | None = None,
    owner_table: str | None = None,
    limit: int = CANDIDATES,
    with_vec: bool | None = None,
) -> tuple[list[Hit], dict]:
    """返回 (命中列表, meta)。向量不可用时自动只走 FTS，并在 meta 里说明。"""
    q = (query or "").strip()
    meta: dict = {"query": q, "k": RRF_K, "fts": 0, "vec": 0, "vec_enabled": False, "degraded": False}
    if not q:
        return [], meta

    if docs is None:
        from app.retrieval.texts import load_docs

        docs = load_docs(db_path, owner_table)
    mapping = doc_map(docs)
    if not mapping:
        return [], meta

    fts_rows = [row for row in fts.search(db_path, q, owner_table, limit) if (row[0], row[1]) in mapping]
    fts_ranking = [(row[0], row[1]) for row in fts_rows]
    fts_rank = {key: i for i, key in enumerate(fts_ranking, 1)}
    fts_score = {(row[0], row[1]): row[2] for row in fts_rows}
    meta["fts"] = len(fts_ranking)

    use_vec = embed.available() if with_vec is None else with_vec
    vec_ranking: list[tuple[str, str]] = []
    vec_rank: dict[tuple[str, str], int] = {}
    vec_score: dict[tuple[str, str], float] = {}
    if use_vec:
        keys, matrix = embed.load_matrix(db_path, owner_table)
        keys = [k for k in keys if k in mapping]
        if matrix is not None and keys:
            matrix = matrix[[i for i, k in enumerate(keys)]]
            qvec = embed.encode([q])
            if qvec is not None:
                sims = embed.similarity(qvec, matrix)
                order = sims.argsort()[::-1][:limit]
                vec_ranking = [keys[i] for i in order]
                vec_rank = {k: i for i, k in enumerate(vec_ranking, 1)}
                vec_score = {keys[i]: float(sims[i]) for i in order}
                meta["vec"] = len(vec_ranking)
                meta["vec_enabled"] = True
    if not meta["vec_enabled"]:
        meta["degraded"] = True

    fused = rrf_fuse([fts_ranking, vec_ranking] if vec_ranking else [fts_ranking], RRF_K)
    hits = [
        Hit(
            doc=mapping[key],
            rrf=score,
            fts_rank=fts_rank.get(key),
            vec_rank=vec_rank.get(key),
            fts_score=fts_score.get(key),
            vec_score=vec_score.get(key),
        )
        for key, score in fused.items()
    ]
    hits.sort(key=lambda h: h.rrf, reverse=True)
    return hits[: max(1, limit)], meta
