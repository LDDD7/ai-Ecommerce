"""SSE 事件：类型与序列化。

事件名与前端约定见 spec §10：plan / tool_call / tool_result / evidence /
diagnosis / critic / playbook / action_draft / token / done / error。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

EVENT_TYPES = (
    "plan",
    "tool_call",
    "tool_result",
    "evidence",
    "diagnosis",
    "critic",
    "playbook",
    "action_draft",
    "token",
    "done",
    "error",
)


@dataclass(slots=True)
class ChatEvent:
    """一条 SSE 事件。`data` 必须是 JSON 可序列化的。"""

    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{self.name}")

    def to_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.name}\ndata: {payload}\n\n"


def to_sse(event: ChatEvent) -> str:
    return event.to_sse()
