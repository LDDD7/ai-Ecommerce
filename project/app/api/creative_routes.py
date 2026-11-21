"""创意工单的 HTTP 层。

五个端点：
- `GET  /api/creative/asset/{draft_id}`  取工单产出的图片（前端 `<img>` 直接引用）
- `POST /api/creative/draft`            生成工单（改图 + 文案 + 合规）
- `GET  /api/creative?sku_id=&limit=`   工单列表（摘要）
- `GET  /api/creative/{draft_id}`       单张工单全文
- `POST /api/creative/{draft_id}/apply` 起草一条换素材动作（进审批队列）

注意路由顺序：`/creative/asset/{draft_id}` 必须写在 `/creative/{draft_id}` 之前，
否则 `asset` 会被当成 draft_id。
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core import auth
from app.core.guards import require_role
from app.creative import service, store
from app.creative.schema import CreativeError, CreativeRequest
from app.core.config import get_settings

router = APIRouter(tags=["creative"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _fail(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


@router.get("/creative/asset/{draft_id}")
def creative_asset(draft_id: str) -> FileResponse:
    settings = get_settings()
    try:
        result = store.get(settings.db_path, draft_id)
    except LookupError as exc:
        raise _fail(404, str(exc)) from exc
    asset = next((a for a in result.assets if a.kind == "image" and a.path), None)
    if not asset:
        raise _fail(404, "这张工单没有产出图片")
    path = Path(asset.path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    # 只允许读 data/creative 下的文件，避免路径穿越
    if PROJECT_ROOT not in resolved.parents or "creative" not in resolved.parts:
        raise _fail(400, "非法的素材路径")
    if not resolved.exists():
        raise _fail(404, "素材文件不存在（可能已被清理）")
    return FileResponse(resolved, media_type="image/png", filename=resolved.name)


@router.post("/creative/draft")
def creative_draft(req: CreativeRequest, _: auth.User = Depends(require_role("manager"))) -> dict:
    settings = get_settings()
    started = time.perf_counter()
    try:
        result = service.draft(settings.db_path, req)
    except CreativeError as exc:
        raise _fail(400, str(exc)) from exc
    except LookupError as exc:
        raise _fail(404, str(exc)) from exc
    body = result.model_dump(mode="json")
    body["meta"] = {"wall_ms": round((time.perf_counter() - started) * 1000, 1)}
    return body


@router.get("/creative")
def creative_list(sku_id: str | None = None, limit: int = 20) -> dict:
    settings = get_settings()
    items = store.list_drafts(settings.db_path, sku_id=sku_id, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/creative/{draft_id}")
def creative_get(draft_id: str) -> dict:
    settings = get_settings()
    try:
        result = store.get(settings.db_path, draft_id)
    except LookupError as exc:
        raise _fail(404, str(exc)) from exc
    return result.model_dump(mode="json")


class CreativeApplyRequest(BaseModel):
    actor: str = Field(default="demo", max_length=40)
    reason: str = Field(default="", max_length=200)


@router.post("/creative/{draft_id}/apply")
def creative_apply(
    draft_id: str, req: CreativeApplyRequest, _: auth.User = Depends(require_role("manager"))
) -> dict:
    settings = get_settings()
    try:
        return service.apply(settings.db_path, draft_id, actor=req.actor, reason=req.reason)
    except LookupError as exc:
        raise _fail(404, str(exc)) from exc
    except CreativeError as exc:
        raise _fail(400, str(exc)) from exc
