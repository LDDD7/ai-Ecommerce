"""检索层：FTS5(trigram) + 本地 bge-m3 向量 → RRF 融合 → 三级重排（spec §20）。"""

from __future__ import annotations

from app.retrieval.hybrid import Hit, rrf_fuse, search
from app.retrieval.texts import Doc, load_docs, load_playbooks, load_policies

__all__ = [
    "Doc",
    "Hit",
    "load_docs",
    "load_playbooks",
    "load_policies",
    "rrf_fuse",
    "search",
]
