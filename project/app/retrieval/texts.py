"""可检索语料的统一视图：平台规则（policy_docs）与策略卡（playbook）。

检索层只认 `Doc`：`owner_table` + `owner_id` 是主键，`index_text()` 是进 FTS/向量的文本。
策略卡在 W2 才落地，`load_playbooks()` 用惰性 import，缺失时返回空列表，不影响规则检索。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

OWNER_POLICY = "policy_docs"
OWNER_PLAYBOOK = "playbook"


@dataclass(slots=True)
class Doc:
    owner_table: str
    owner_id: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.owner_table, self.owner_id)

    def index_text(self) -> str:
        """进索引的正文：标题权重靠前，标签补在末尾，都是自然语言。"""
        parts = [self.title, self.body]
        if self.tags:
            parts.append("关键词：" + "、".join(self.tags))
        return "\n".join(p.strip() for p in parts if p and p.strip())

    def snippet(self, limit: int = 120) -> str:
        text = self.body.replace("\n", " ").strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def as_dict(self) -> dict:
        return {
            "owner_table": self.owner_table,
            "owner_id": self.owner_id,
            "title": self.title,
            "body": self.body,
            "snippet": self.snippet(),
            "tags": list(self.tags),
        }


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在：{path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_policies(db_path: str | Path) -> list[Doc]:
    with _connect(db_path) as con:
        rows = con.execute("SELECT doc_id, title, body, tags FROM policy_docs ORDER BY doc_id").fetchall()
    docs: list[Doc] = []
    for doc_id, title, body, tags in rows:
        tag_list = [t.strip() for t in str(tags or "").split(",") if t.strip()]
        docs.append(Doc(OWNER_POLICY, str(doc_id), str(title), str(body), tag_list))
    return docs


def load_playbooks() -> list[Doc]:
    """策略卡来自 YAML（app/playbook/cards/*.yml），W2 才有；这里只做适配。"""
    try:
        from app.playbook.loader import load_cards  # 惰性 import，避免循环依赖
    except Exception:  # pragma: no cover - 策略卡模块尚未落地
        return []

    docs: list[Doc] = []
    for card in load_cards():
        docs.append(
            Doc(
                OWNER_PLAYBOOK,
                card.card_id,
                card.title,
                card.search_text(),
                list(card.root_causes),
                {
                    "risk": card.risk,
                    "scope": card.scope,
                    "action": card.action_type,
                    "metrics": list(card.metrics),
                    "root_causes": list(card.root_causes),
                    "window_days": card.expected_effect.window_days,
                },
            )
        )
    return docs


def load_docs(db_path: str | Path, owner_table: str | None = None) -> list[Doc]:
    if owner_table == OWNER_POLICY:
        return load_policies(db_path)
    if owner_table == OWNER_PLAYBOOK:
        return load_playbooks()
    return load_policies(db_path) + load_playbooks()


def doc_map(docs: list[Doc]) -> dict[tuple[str, str], Doc]:
    return {doc.key: doc for doc in docs}
