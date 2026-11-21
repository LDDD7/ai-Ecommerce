"""促销日历接口（spec §28）。

- `GET    /api/promo`            日历 + 促销天数概览（登录可见）
- `POST   /api/promo`            新建一段（owner）
- `PATCH  /api/promo/{id}`       改一段（owner）
- `DELETE /api/promo/{id}`       删一段（owner）
- `POST   /api/promo/rebuild`    手动重算 `dim_date`（owner，兜底用）

写操作成功后都会立刻把日历回写 `dim_date`，不需要另外「生效」一步。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import promo
from app.core.config import get_settings
from app.core.guards import current_user, require_role

router = APIRouter()


class PeriodCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    start_dt: str
    end_dt: str
    kind: str = "campaign"
    note: str = ""


class PeriodPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    start_dt: str | None = None
    end_dt: str | None = None
    kind: str | None = None
    note: str | None = None


class RebuildRequest(BaseModel):
    start: str | None = None
    end: str | None = None


def _db() -> str:
    return get_settings().db_path


def _sync(db_path: str, *ranges: tuple[str, str]) -> dict[str, Any]:
    """按日历重算，并把本次涉及的范围一起纳入。"""
    cal_start, cal_end = promo.calendar_span(db_path)
    if not cal_start:
        return promo.rebuild_flags(db_path, force=False)
    start = min([cal_start] + [r[0] for r in ranges])
    end = max([cal_end] + [r[1] for r in ranges])
    return promo.rebuild_flags(db_path, start=start, end=end, force=True)


def _bad(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "invalid_period", "message": str(exc)})


@router.get("/promo")
def promo_list(user: Any = Depends(current_user)) -> dict[str, Any]:
    db = _db()
    promo.ensure_schema(db)
    return promo.summary(db)


@router.post("/promo", status_code=201)
def promo_create(payload: PeriodCreate, user: Any = Depends(require_role("owner"))) -> dict[str, Any]:
    db = _db()
    promo.ensure_schema(db)
    try:
        period = promo.create_period(
            db,
            name=payload.name,
            start_dt=payload.start_dt,
            end_dt=payload.end_dt,
            kind=payload.kind,
            note=payload.note,
        )
    except ValueError as exc:
        raise _bad(exc) from exc
    sync = _sync(db, (period["start_dt"], period["end_dt"]))
    return {"period": period, "sync": sync}


@router.patch("/promo/{promo_id}")
def promo_update(
    promo_id: str,
    payload: PeriodPatch,
    user: Any = Depends(require_role("owner")),
) -> dict[str, Any]:
    db = _db()
    before = promo.get_period(db, promo_id)
    try:
        period = promo.update_period(
            db,
            promo_id,
            name=payload.name,
            start_dt=payload.start_dt,
            end_dt=payload.end_dt,
            kind=payload.kind,
            note=payload.note,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "promo_not_found", "message": str(exc)}) from exc
    except ValueError as exc:
        raise _bad(exc) from exc
    ranges = [(period["start_dt"], period["end_dt"])]
    if before:
        ranges.append((before["start_dt"], before["end_dt"]))
    sync = _sync(db, *ranges)
    return {"period": period, "sync": sync}


@router.delete("/promo/{promo_id}")
def promo_delete(promo_id: str, user: Any = Depends(require_role("owner"))) -> dict[str, Any]:
    db = _db()
    try:
        removed = promo.delete_period(db, promo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "promo_not_found", "message": str(exc)}) from exc
    # 删掉最后一段时日历会变空，rebuild 会保守跳过，这里用被删的范围兜底清干净
    sync = _sync(db, (removed["start_dt"], removed["end_dt"]))
    if not sync.get("rebuilt"):
        cleared = promo.clear_range(db, removed["start_dt"], removed["end_dt"])
        sync = {"rebuilt": True, "cleared": cleared, "promo_days": 0}
    return {"removed": removed, "sync": sync}


@router.post("/promo/rebuild")
def promo_rebuild(payload: RebuildRequest, user: Any = Depends(require_role("owner"))) -> dict[str, Any]:
    db = _db()
    promo.ensure_schema(db)
    try:
        result = promo.rebuild_flags(db, start=payload.start, end=payload.end, force=True)
    except ValueError as exc:
        raise _bad(exc) from exc
    return {"sync": result}
