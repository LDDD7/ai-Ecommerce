"""数据导入接口：上传 → 预览（不动库）→ 确认入库。

商家侧只有三个动作：选文件、改几个字段映射、点「确认入库」。
接口按同样顺序排：`/schema`（要什么）、`/preview`（认出了什么）、`/commit`（写进去）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core import auth
from app.core.config import get_settings
from app.core.guards import require_role
from app.ingest import quality, service, store

router = APIRouter(tags=["ingest"])


def _root() -> Path:
    return Path(get_settings().db_path).resolve().parents[1]


def _db() -> str:
    return str(get_settings().db_path)


@router.get("/ingest/schema")
def ingest_schema() -> dict[str, Any]:
    """四个数据集、各自的字段与中文别名，前端用来渲染映射表。"""
    return service.schema_payload()


@router.post("/ingest/preview")
async def ingest_preview(
    file: UploadFile = File(...),
    dataset: str | None = Form(default=None),
    mapping: str | None = Form(default=None),
    _: auth.User = Depends(require_role("owner")),
) -> dict[str, Any]:
    data = await file.read()
    parsed: dict[str, str] | None = None
    if mapping:
        try:
            raw = json.loads(mapping)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid_mapping") from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="invalid_mapping")
        parsed = {str(k): str(v) for k, v in raw.items()}
    try:
        return service.save_preview(
            data,
            file.filename or "upload.csv",
            dataset=dataset or None,
            mapping=parsed,
            root=_root(),
        )
    except service.PreviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ingest/commit")
def ingest_commit(
    payload: dict[str, Any], _: auth.User = Depends(require_role("owner"))
) -> dict[str, Any]:
    token = str(payload.get("token") or "")
    if not token:
        raise HTTPException(status_code=400, detail="missing_token")
    try:
        return service.commit_preview(
            token,
            db_path=_db(),
            root=_root(),
            allow_warn=bool(payload.get("allow_warn", True)),
            confirm_changes=bool(payload.get("confirm_changes", False)),
        )
    except store.ChangesNeedConfirm as exc:
        # 409 而不是 400：不是请求错了，是「需要你再点一次确认」
        raise HTTPException(
            status_code=409,
            detail={
                "code": "changes_need_confirm",
                "message": str(exc),
                "diff": exc.diff,
            },
        ) from exc
    except service.PreviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ingest/undo")
def ingest_undo(
    payload: dict[str, Any], _: auth.User = Depends(require_role("owner"))
) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise HTTPException(status_code=400, detail="missing_run_id")
    try:
        return service.undo_run(run_id, db_path=_db())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "undo_refused", "message": str(exc)}) from exc


@router.get("/ingest/runs")
def ingest_runs(limit: int = 20) -> dict[str, Any]:
    items = store.recent_runs(_db(), limit=max(1, min(int(limit), 100)))
    return {"items": items, "count": len(items)}


@router.get("/ingest/capabilities")
def ingest_capabilities() -> dict[str, Any]:
    """当前库里有哪些数据、还缺哪些指标——入库后立刻能看到「多了什么」。"""
    return quality.capabilities(_db())
