"""素材诊断（D9）：把主图/详情页读成结构化属性，再折算成诊断证据。

对外只暴露 `vis` 一个入口面：`MaterialFeatures` / `inspect` / `compare` / `findings`。
"""

from __future__ import annotations

from app.vision.schema import (
    ISSUE_SPECS,
    PROMPT_VERSION,
    MaterialFeatures,
    MaterialIssue,
)

__all__ = ["ISSUE_SPECS", "PROMPT_VERSION", "MaterialFeatures", "MaterialIssue"]
