"""创意工单的落库层。

只做三件事：建表、存一张工单、按 id/sku 取。工单本身不改任何状态——
真正生效要等 `service.apply()` 起草一条 `swap_creative` 动作，再走审批。
"""

from __future__ import annotations

import json
import os

from app.creative.schema import CreativeError, CreativeResult

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS creative_draft (
    draft_id     TEXT PRIMARY KEY,
    sku_id       TEXT NOT NULL,
    provider     TEXT NOT NULL,
    status       TEXT NOT NULL,
    target       TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
)
"""

INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_creative_draft_sku ON creative_draft(sku_id, created_at DESC)",
)


def ensure_table(db_path: str) -> None:
    """建表是幂等的；老库第一次调 GET/POST 时补上。"""
    import sqlite3

    if not os.path.exists(db_path):
        raise CreativeError(f"数据库不存在：{db_path}")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(TABLE_DDL)
        for ddl in INDEX_DDL:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def save(db_path: str, result: CreativeResult, *, request: dict | None = None) -> None:
    import sqlite3
    from datetime import datetime, timezone

    ensure_table(db_path)
    payload = json.dumps(request or {}, ensure_ascii=False)
    body = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO creative_draft"
            "(draft_id, sku_id, provider, status, target, request_json, result_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                result.draft_id,
                result.sku_id,
                result.provider,
                result.status,
                result.target,
                payload,
                body,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get(db_path: str, draft_id: str) -> CreativeResult:
    import sqlite3

    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT result_json FROM creative_draft WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise LookupError(f"创意工单不存在：{draft_id}")
    return CreativeResult.model_validate(json.loads(row[0]))


def list_drafts(db_path: str, *, sku_id: str | None = None, limit: int = 20) -> list[dict]:
    """列表只返回摘要（不含整份 result），避免一次把几十张工单的文案全拖给前端。"""
    import sqlite3

    ensure_table(db_path)
    limit = max(1, min(int(limit), 100))
    sql = (
        "SELECT draft_id, sku_id, provider, status, target, result_json, created_at"
        " FROM creative_draft"
    )
    args: tuple = ()
    if sku_id:
        sql += " WHERE sku_id = ?"
        args = (sku_id,)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args = args + (limit,)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    items: list[dict] = []
    for draft_id, sku, provider, status, target, result_json, created in rows:
        body = json.loads(result_json)
        items.append(
            {
                "draft_id": draft_id,
                "sku_id": sku,
                "provider": provider,
                "status": status,
                "target": target,
                "created_at": created,
                "fixes": len(body.get("fixes") or []),
                "issues_left": (body.get("after") or {}).get("issues_left") or [],
                "after_url": (body.get("after") or {}).get("image_url", ""),
            }
        )
    return items
