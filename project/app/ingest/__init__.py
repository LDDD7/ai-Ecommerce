"""数据接入：把商家自己的导出文件变成我们的事实/维度表。

对外只暴露两件事——`save_preview`（只认不改）与 `commit_preview`（写库并记批次），
外加 `quality.capabilities` 告诉商家「这份数据能让哪些指标算出来」。
"""

from __future__ import annotations

from app.ingest.canonical import CAPABILITIES, DATASETS, canonical_payload
from app.ingest.service import (
    PreviewError,
    commit_preview,
    load_preview,
    save_preview,
    schema_payload,
    undo_run,
)
from app.ingest.store import ChangesNeedConfirm

__all__ = [
    "CAPABILITIES",
    "DATASETS",
    "ChangesNeedConfirm",
    "PreviewError",
    "canonical_payload",
    "commit_preview",
    "load_preview",
    "save_preview",
    "schema_payload",
    "undo_run",
]
