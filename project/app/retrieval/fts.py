"""FTS5（trigram）全文检索：中文不分词也能用，代价是查询串短于 3 个字要退化。

表结构与 spec §20.2 一致：`search_fts(owner_table UNINDEXED, owner_id UNINDEXED, body)`。
`ensure()` 在已有库上补建表，这样不用重跑 seed 也能用。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.retrieval.texts import Doc

FTS_TABLE = "search_fts"

DDL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
    owner_table UNINDEXED,
    owner_id    UNINDEXED,
    body,
    tokenize = 'trigram'
);
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在：{path}")
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def ensure(db_path: str | Path) -> None:
    con = _connect(db_path)
    try:
        con.executescript(DDL)
        con.commit()
    finally:
        con.close()


def index(db_path: str | Path, docs: list[Doc], replace: bool = True) -> int:
    """把文档写进 FTS。replace=True 时先清掉这些 owner 的旧行（幂等）。"""
    if not docs:
        return 0
    con = _connect(db_path)
    try:
        con.executescript(DDL)
        if replace:
            con.executemany(
                f"DELETE FROM {FTS_TABLE} WHERE owner_table = ? AND owner_id = ?",
                [(d.owner_table, d.owner_id) for d in docs],
            )
        con.executemany(
            f"INSERT INTO {FTS_TABLE} (owner_table, owner_id, body) VALUES (?, ?, ?)",
            [(d.owner_table, d.owner_id, d.index_text()) for d in docs],
        )
        con.commit()
        return len(docs)
    finally:
        con.close()


def count(db_path: str | Path) -> int:
    con = _connect(db_path)
    try:
        con.executescript(DDL)
        return int(con.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0])
    finally:
        con.close()


def _quote(query: str) -> str:
    """把用户输入包成短语查询，避免 FTS5 语法字符（- * : 等）被当成运算符。"""
    cleaned = query.replace('"', " ").strip()
    return f'"{cleaned}"'


_SPLIT = re.compile(r"[\s,，。;；、!?！？:：()（）\[\]【】\"'“”‘’/\\|]+")


def _segments(query: str) -> list[str]:
    return [seg for seg in _SPLIT.split(query) if seg]


def _trigrams(seg: str) -> list[str]:
    return [seg[i : i + 3] for i in range(len(seg) - 2)]


def build_match(query: str) -> str | None:
    """中文自然语言查询 → FTS5 查询式。

    trigram 分词器把文本切成 3 字滑窗，`MATCH '"整句"'` 只有在原文**连续出现整句**时才命中，
    所以长句必须拆成三元组再取 OR：命中越多，bm25 排名越靠前。
    """
    terms: list[str] = []
    for seg in _segments(query):
        if len(seg) < 3:
            continue
        terms.append(_quote(seg))
        terms.extend(_quote(g) for g in _trigrams(seg))
    if not terms:
        return None
    return " OR ".join(dict.fromkeys(terms))


def search(
    db_path: str | Path,
    query: str,
    owner_table: str | None = None,
    limit: int = 12,
) -> list[tuple[str, str, float]]:
    """返回 [(owner_table, owner_id, score)]，score 越大越相关（bm25 取负）。"""
    q = (query or "").strip()
    if not q:
        return []
    con = _connect(db_path)
    try:
        con.executescript(DDL)
        limit = max(1, min(int(limit), 100))
        where = "WHERE owner_table = ?" if owner_table else ""
        params: list[object] = [owner_table] if owner_table else []
        match = build_match(q)
        if match:
            sql = (
                f"SELECT owner_table, owner_id, bm25({FTS_TABLE}) AS score FROM {FTS_TABLE} "
                f"{where} {'AND' if where else 'WHERE'} {FTS_TABLE} MATCH ? "
                "ORDER BY score LIMIT ?"
            )
            try:
                rows = con.execute(sql, [*params, match, limit]).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                return [(r["owner_table"], r["owner_id"], -float(r["score"])) for r in rows]
        # trigram 对 <3 字符（以及三元组全落空时）退化成子串匹配，保证短词「尺码」也有结果。
        #
        # 这里不能用 SQL 的 LIKE：trigram 分词器的 FTS5 表把 LIKE 交给三元组索引处理，
        # 模式短于 3 个字时索引查不出任何行（SQLite 3.45 实测返回 0 行，3.37 才会全表扫）。
        # 所以取回正文在 Python 里做子串判断——行数很少（策略卡 + 政策文档），代价可忽略。
        like_terms = [t for t in _segments(q)][:4] or [q]
        sql = f"SELECT owner_table, owner_id, body FROM {FTS_TABLE} {where}"
        rows = con.execute(sql, params).fetchall()
        hits: list[tuple[str, str, float]] = []
        for row in rows:
            body = row["body"] or ""
            score = sum(body.count(term) for term in like_terms)
            if score:
                hits.append((row["owner_table"], row["owner_id"], float(score)))
        # 命中次数多的排前面（短词没有 bm25，只能这样分个轻重）
        hits.sort(key=lambda item: item[2], reverse=True)
        return hits[:limit]
    finally:
        con.close()
