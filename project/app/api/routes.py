"""API 路由：健康检查、指标目录、QuerySpec 查询、概览、诊断。"""

from __future__ import annotations

import logging
import copy
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core import auth
from app.core.cache import get_cache
from app.core.config import get_settings
from app.core.guards import actor_of, require_role
from app.diagnose import build_diagnosis
from app.insights import build_overview
from app.metrics.compiler import run as run_query
from app.metrics.spec import MAX_DAYS, MAX_DIMS, MAX_FILTERS, MAX_METRICS, MAX_ROWS, MetricError, QuerySpec, load_catalog
from app.orchestrator import ChatEvent, run_chat
from app.playbook.search import search_for_codes, search_playbook

router = APIRouter()
logger = logging.getLogger(__name__)


def _cache_key(*parts: Any) -> tuple:
    """缓存键：参数 + 数据库 mtime（数据重建后自动失效）。"""
    db_path = str(get_settings().db_path)
    try:
        mtime = Path(db_path).stat().st_mtime
    except OSError:
        mtime = 0.0
    return (db_path, round(mtime, 3)) + tuple(parts)


@router.get("/health")
def health() -> dict[str, Any]:
    from app.core import runtime

    settings = get_settings()
    db = Path(settings.db_path)
    active = runtime.llm_active()
    info: dict[str, Any] = {
        "status": "ok",
        "env": settings.app_env,
        "db": {"path": str(db), "exists": db.exists()},
        "llm": {
            "configured": bool(runtime.value("llm_api_key").strip()),
            "active": active,
            "model_planner": runtime.model_for("planner"),
            "planner": "llm-v1" if active == "real" else "rule-v1",
        },
        "catalog_version": load_catalog().version,
        "metrics_count": len(load_catalog().metrics),
    }
    if db.exists():
        import sqlite3

        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            info["db"]["orders"] = con.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]
            info["db"]["last_dt"] = con.execute("SELECT MAX(dt) FROM dim_date").fetchone()[0]
        finally:
            con.close()
    else:
        info["status"] = "degraded"
        info["hint"] = "先跑 python -m app.data.seed 生成数据"
    return info


@router.get("/llm/probe")
def llm_probe() -> dict[str, Any]:
    """探活：真发一次最小请求，确认 key / 网关 / 模型名都能用。

    不放进 /health：健康检查要快且不花钱，探活是显式动作。
    """
    settings = get_settings()
    from app.core import runtime

    active = runtime.llm_active()
    if active != "real":
        return {
            "ok": False,
            "active": "mock",
            "reason": "LLM_MODE 非 real 或未配置 LLM_API_KEY，当前走规则版（链路仍可用）",
        }
    try:
        from app.core.llm import get_client, usage_stats
    except Exception as exc:  # noqa: BLE001 - 网关模块缺失也要给出可读原因
        return {"ok": False, "active": "real", "reason": f"网关未就绪：{type(exc).__name__}: {exc}"}

    t0 = time.perf_counter()
    try:
        result = get_client().chat(
            [{"role": "user", "content": "只回四个字：链路正常"}],
            max_tokens=16,
            use_cache=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "active": "real",
            "reason": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    return {
        "ok": True,
        "active": "real",
        "model": result.model,
        "text": (getattr(result, "text", "") or "").strip()[:40],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "tokens": {
            "prompt": getattr(result, "prompt_tokens", 0),
            "completion": getattr(result, "completion_tokens", 0),
        },
        "usage": usage_stats(),
    }


@router.get("/metrics/catalog")
def catalog() -> dict[str, Any]:
    cat = load_catalog()
    return {
        "version": cat.version,
        "owner": cat.owner,
        "metrics": cat.public(),
        "limits": {
            "max_metrics": MAX_METRICS,
            "max_dimensions": MAX_DIMS,
            "max_filters": MAX_FILTERS,
            "max_rows": MAX_ROWS,
            "max_days": MAX_DAYS,
        },
    }


class QueryRequest(BaseModel):
    spec: QuerySpec


class QueryNLRequest(BaseModel):
    """自然语言查数：一句话 → QuerySpec → 直接跑出结果。"""

    question: str = Field(min_length=1, max_length=300)
    as_of: str | None = None
    window_days: int = 7
    use_llm: bool = True


@router.post("/query/nl")
def query_nl(req: QueryNLRequest) -> dict[str, Any]:
    settings = get_settings()
    if not 1 <= req.window_days <= 30:
        raise HTTPException(status_code=400, detail={"error": "invalid_window", "message": "window_days 必须在 1~30 之间"})
    from app.insights.overview import resolve_as_of
    from app.query.nl import parse_question

    t0 = time.perf_counter()
    try:
        as_of = resolve_as_of(settings.db_path, req.as_of).isoformat()
    except MetricError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_query", "message": str(e)}) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_question", "message": str(e)}) from None

    # 同一句话 + 同一基准日只解析一次：LLM 解析约 3~5s，演示时不该反复等。
    cache_key = _cache_key("query_nl", req.question.strip(), as_of, req.window_days, req.use_llm)

    def _parse() -> dict[str, Any]:
        parsed = parse_question(
            req.question,
            as_of=as_of,
            window_days=req.window_days,
            use_llm=req.use_llm,
        )
        return parsed.as_dict()

    try:
        parsed_body, cached = get_cache().get_or_compute(cache_key, _parse)
    except MetricError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_query", "message": str(e)}) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_question", "message": str(e)}) from None

    body: dict[str, Any] = copy.deepcopy(parsed_body)
    try:
        result = run_query(QuerySpec(**body["spec"]), settings.db_path)
        body["result"] = result.as_dict()
    except MetricError as e:
        body["result"] = None
        body["warnings"] = [*body.get("warnings", []), f"查询未执行：{e}"]
    body["as_of"] = as_of
    body["meta"] = {"wall_ms": round((time.perf_counter() - t0) * 1000, 1), "cached": cached}
    return body


@router.get("/overview")
def overview(days: int = 7, as_of: str | None = None) -> dict[str, Any]:
    """体检台一屏所需数据：KPI、健康分、异常卡、趋势、款式分布。

    `days` 为当前窗口长度（与紧邻的等长上期对比），限制在 1~30。
    `as_of` 为基准日，缺省用配置里的 demo_as_of（默认 2026-11-17）。
    """
    if not 1 <= days <= 30:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_window", "message": "days 必须在 1..30 之间"},
        )
    settings = get_settings()
    t0 = time.perf_counter()
    try:
        cached_body, cached = get_cache().get_or_compute(
            _cache_key("overview", settings.demo_as_of, as_of, days),
            lambda: build_overview(settings.db_path, window_days=days, as_of=as_of),
        )
    except MetricError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_query", "message": str(e)}) from None
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_date", "message": "as_of 必须是 YYYY-MM-DD 格式"},
        ) from None
    body = copy.deepcopy(cached_body)  # 别让调用方的 meta 改动污染缓存
    body["meta"]["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    body["meta"]["cached"] = cached
    return body


class DiagnoseRequest(BaseModel):
    """诊断入参。全部可选：不传就用配置里的演示基准日与 7 天窗口。"""

    as_of: str | None = None
    window_days: int = 7
    max_cards: int = 5
    # rules=规则版反方审查（约 0.6s）；llm=让模型再反驳一遍（只降不升，约 +2s）。
    critic: str | None = None
    # 素材诊断：给 SKU 级诊断卡补上「主图里看到什么」（R2/R4 才有意义）。
    material: bool = False
    # 第二轮复审：同 scope 扩到 14 天找必需辅证（约 +0.85s）。消融时可关。
    replan: bool = True


def _run_diagnosis(req: DiagnoseRequest) -> dict[str, Any]:
    if not 1 <= req.window_days <= 30:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_window", "message": "window_days 必须在 1..30 之间"},
        )
    if not 1 <= req.max_cards <= 10:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_window", "message": "max_cards 必须在 1..10 之间"},
        )
    if req.critic not in (None, "rules", "llm"):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_critic", "message": "critic 只能是 rules 或 llm"},
        )
    settings = get_settings()
    t0 = time.perf_counter()
    cache_key = _cache_key("diagnose", settings.db_path, settings.demo_as_of,
                           req.as_of, req.window_days, req.max_cards, req.critic, req.material, req.replan)
    try:
        cached_body, cached = get_cache().get_or_compute(
            cache_key,
            lambda: build_diagnosis(
                settings.db_path,
                as_of=req.as_of,
                window_days=req.window_days,
                max_cards=req.max_cards,
                use_llm_critic=(req.critic == "llm"),
                with_material=req.material,
                with_replan=req.replan,
            ),
        )
    except MetricError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_query", "message": str(e)}) from None
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_date", "message": "as_of 必须是 YYYY-MM-DD 格式"},
        ) from None
    body = copy.deepcopy(cached_body)
    body["meta"]["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    body["meta"]["cached"] = cached
    return body


@router.post("/diagnose")
def diagnose(req: DiagnoseRequest | None = None) -> dict[str, Any]:
    """一次完整诊断：异常检测 → 维度贡献 → 根因假设 → 诊断卡 + 分解 + 漏斗。"""
    return _run_diagnosis(req or DiagnoseRequest())


@router.get("/diagnose")
def diagnose_get(
    as_of: str | None = None,
    window_days: int = 7,
    max_cards: int = 5,
    critic: str | None = None,
    material: bool = False,
    replan: bool = True,
) -> dict[str, Any]:
    """同一套诊断的 GET 版本，方便 curl / 浏览器直接看。"""
    return _run_diagnosis(
        DiagnoseRequest(
            as_of=as_of,
            window_days=window_days,
            max_cards=max_cards,
            critic=critic,
            material=material,
            replan=replan,
        )
    )


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    as_of: str | None = None
    window_days: int = 7


@router.post("/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    """问一句 → 一屏事件流（SSE）。前端用 fetch + ReadableStream 读。"""
    if not 1 <= req.window_days <= 30:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_window", "message": "window_days 必须在 1..30 之间"},
        )

    async def gen():
        try:
            async for ev in run_chat(req.message, as_of=req.as_of, window_days=req.window_days):
                yield ev.to_sse()
        except Exception as exc:  # noqa: BLE001 - 流已开始，只能把错误当事件发出去
            yield ChatEvent("error", {"message": f"{type(exc).__name__}: {exc}"}).to_sse()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


@router.post("/query")
def query(req: QueryRequest) -> dict[str, Any]:
    settings = get_settings()
    t0 = time.perf_counter()
    try:
        result = run_query(req.spec, settings.db_path)
    except MetricError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_query", "message": str(e)}) from None
    body = result.as_dict()
    body["spec"] = req.spec.model_dump(mode="json")
    body["meta"]["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return body


PLAYBOOK_MODES = ("off", "rule", "cross", "llm")


class PlaybookSearchRequest(BaseModel):
    """策略卡检索入参：query 走混合召回，root_causes 走按根因直达（诊断卡用后者）。"""

    query: str = Field(default="", max_length=200)
    root_causes: list[str] = Field(default_factory=list, max_length=5)
    metrics: list[str] = Field(default_factory=list, max_length=6)
    risk_max: int = 2
    limit: int = 5
    mode: str | None = None


@router.post("/playbook/search")
def playbook_search(req: PlaybookSearchRequest) -> dict[str, Any]:
    """自然语言或根因码 → 候选策略卡（带命中理由与预期效果）。"""
    if not 0 <= req.risk_max <= 2:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_risk", "message": "risk_max 必须在 0..2 之间"},
        )
    if req.mode is not None and req.mode not in PLAYBOOK_MODES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_mode", "message": f"mode 只允许 {list(PLAYBOOK_MODES)}"},
        )

    settings = get_settings()
    t0 = time.perf_counter()
    q = req.query.strip()
    try:
        if q:
            hits, meta = search_playbook(
                settings.db_path,
                q,
                root_causes=req.root_causes,
                metrics=req.metrics,
                risk_max=req.risk_max,
                limit=req.limit,
                mode=req.mode,
            )
        elif req.root_causes:
            hits, meta = search_for_codes(
                settings.db_path,
                req.root_causes,
                risk_max=req.risk_max,
                limit=req.limit,
            )
            meta["mode"] = "by_root"
        else:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_query", "message": "query 与 root_causes 至少要给一个"},
            )
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "index_missing",
                "message": f"检索索引不可用：{exc}",
                "hint": "先跑 python -m tools.index_vectors 建 FTS/向量索引",
            },
        ) from None

    meta["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return {"hits": [hit.as_dict() for hit in hits], "meta": meta}


# ---------------------------------------------------------------- 素材诊断（D9）

VISION_ROLES = ("main", "detail", "scene")
VISION_MODES = ("auto", "mock", "vlm")


def _images_dir() -> Path:
    """素材图与库放在一起：`data/store.db` -> `data/images/`。"""
    return Path(get_settings().db_path).resolve().parent / "images"


@router.get("/images/{sku_id}")
def image(sku_id: str, role: str = "main") -> FileResponse:
    """返回合成素材图（PNG）。前端卡片缩略图与 vision.inspect 都走这里。"""
    if role not in VISION_ROLES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_role", "message": f"role 只允许 {list(VISION_ROLES)}"},
        )
    path = _images_dir() / f"{sku_id}_{role}.png"
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"error": "image_missing", "message": f"没有 {sku_id} 的 {role} 素材图", "path": str(path)},
        )
    return FileResponse(path, media_type="image/png", headers={"cache-control": "public, max-age=3600"})


class VisionInspectRequest(BaseModel):
    """素材检查入参：不给 mode 就按 settings.vision_mode 决定现算还是回放。"""

    sku_id: str = Field(min_length=3, max_length=32)
    role: str = "main"
    mode: str | None = None


@router.post("/vision/inspect")
def vision_inspect(req: VisionInspectRequest) -> dict[str, Any]:
    """读一张图，返回结构化素材属性 + 问题码 + 折算后的根因与建议动作。"""
    if req.role not in VISION_ROLES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_role", "message": f"role 只允许 {list(VISION_ROLES)}"},
        )
    if req.mode is not None and req.mode not in VISION_MODES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_mode", "message": f"mode 只允许 {list(VISION_MODES)}"},
        )

    t0 = time.perf_counter()
    try:
        from app.vision.inspector import inspect  # 延迟 import：没有 torch/模型时模块仍可加载
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail={"error": "vision_unavailable", "message": f"素材检查模块不可用：{exc}"},
        ) from None

    try:
        features, meta = inspect(get_settings().db_path, req.sku_id, req.role, mode=req.mode)
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "sku_not_found", "message": str(exc)},
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_mode", "message": str(exc)},
        ) from None

    meta["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "sku_id": features.sku_id,
        "role": features.role,
        "features": features.as_dict(),
        "issues": [i.model_dump() for i in features.issues()],
        "evidence": features.to_evidence(),
        "image_url": f"/api/images/{features.sku_id}?role={features.role}",
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# 设置：页面上填 key / 地址 / 模型
# ---------------------------------------------------------------------------
# 三条硬规则（别改）：
#   1. 读接口永不回明文 key，只回掩码；
#   2. 写的是本地 SQLite（data/store.db 的 app_setting 表），不碰 .env；
#   3. 保存后调 runtime.refresh()，下一次请求立刻用新配置，不用重启。


class SettingsUpdate(BaseModel):
    """全部字段可选；没传=不改，传空串=删除这条覆盖（回到 .env）。"""

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model_planner: str | None = None
    llm_model_fast: str | None = None
    llm_model_vision: str | None = None
    llm_mode: str | None = None
    creative_provider: str | None = None
    dashscope_api_key: str | None = None
    creative_image_model: str | None = None
    creative_edit_model: str | None = None
    creative_adapter: str | None = None


def _settings_payload() -> dict[str, Any]:
    from app.core import runtime
    from app.core.settings_store import CREATIVE_PROVIDERS, OVERRIDE_FIELDS, PROVIDERS

    body = runtime.snapshot()
    body["providers"] = list(PROVIDERS)
    body["creative_providers"] = list(CREATIVE_PROVIDERS)
    body["overridable"] = list(OVERRIDE_FIELDS)
    body["env_file"] = str(Path(get_settings().db_path).parent / ".env")
    return body


@router.get("/settings")
def get_settings_api(_: auth.User = Depends(require_role("owner"))) -> dict[str, Any]:
    return _settings_payload()


@router.put("/settings")
def put_settings(req: SettingsUpdate, _: auth.User = Depends(require_role("owner"))) -> dict[str, Any]:
    from app.core import runtime
    from app.core.settings_store import clear_all, validate, write_many

    provided = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if not provided:
        raise HTTPException(status_code=400, detail={"error": "empty_update", "message": "没有要保存的字段"})
    errors = validate(provided)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_setting", "message": "；".join(errors), "errors": errors},
        )

    db_path = get_settings().db_path
    write_many(db_path, provided)
    runtime.refresh()
    # 审计：只记字段名，绝不记值（值里可能有 key）
    logger.info("settings.update fields=%s", sorted(provided.keys()))
    return _settings_payload()


@router.post("/settings/reset")
def reset_settings(_: auth.User = Depends(require_role("owner"))) -> dict[str, Any]:
    from app.core import runtime
    from app.core.settings_store import clear_all

    clear_all(get_settings().db_path)
    runtime.refresh()
    logger.info("settings.reset（已回到 .env 默认）")
    return _settings_payload()


class SettingsTestRequest(BaseModel):
    # 允许「先测再用」：不传 key 就用当前生效值
    target: str = Field(default="chat", pattern="^(chat|creative)$")
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


@router.post("/settings/test")
def test_settings(req: SettingsTestRequest, _: auth.User = Depends(require_role("owner"))) -> dict[str, Any]:
    """用 1 个 token 验 key：不生成图片、不跑诊断，几乎零成本。"""
    from app.core import runtime
    from app.core.llm import LLMClient, LLMError

    if req.target == "creative":
        key = (req.api_key or runtime.dashscope_key()).strip()
        if not key:
            return {"ok": False, "target": "creative", "reason": "还没填生图 Key（百炼）"}
        base_url = (req.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
        model = (req.model or "qwen3.8-flash").strip()
        note = "这是一次文本调用，只验 key 是否有效；生图接口在阶段 2 才接。"
    else:
        key = (req.api_key or runtime.value("llm_api_key")).strip()
        if not key:
            return {"ok": False, "target": "chat", "reason": "还没填对话模型 Key"}
        base_url = (req.base_url or runtime.value("llm_base_url")).strip()
        model = (req.model or runtime.model_for("fast")).strip()
        note = ""

    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return {"ok": False, "target": req.target, "reason": "API 地址必须以 http:// 或 https:// 开头"}

    t0 = time.perf_counter()
    try:
        client = LLMClient(
            base_url=base_url,
            api_key=key,
            cache=False,
            timeout_s=10.0,
            max_retries=0,
        )
        result = client.chat(
            [{"role": "user", "content": "只回两个字：正常"}],
            model=model,
            max_tokens=8,
            use_cache=False,
        )
    except LLMError as exc:
        return {
            "ok": False,
            "target": req.target,
            "reason": str(exc),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "note": note,
        }
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权都会到这里
        return {
            "ok": False,
            "target": req.target,
            "reason": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "note": note,
        }

    return {
        "ok": True,
        "target": req.target,
        "model": result.model,
        "text": (getattr(result, "text", "") or "").strip()[:40],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "tokens": {"prompt": result.prompt_tokens, "completion": result.completion_tokens},
        "note": note,
    }
