"""三级重排（spec §20.4）：规则分 → cross-encoder → LLM listwise。

分工：

- `rule_score()` 是默认档，纯算术、<1ms，`breakdown` 原样进 trace——回答「为什么选这张卡」时
  能指着数字说话；
- cross-encoder（`bge-reranker-v2-m3`）在本模块里当**过滤器**用：top_n 候选逐对打分，
  `<0.2` 判「不相关」直接丢，通过的候选再把 `0.5 * ce` 叠回规则分；
- `mode="llm"` 本模块不真调 LLM（listwise 留给上层 orchestrator），按 `cross` 执行并在 meta
  标 `llm_requested=True`——这样「重排不能成为单点故障」这条底线不依赖 key。

降级策略（spec §20.8）：模型路径为空/不存在、import 失败、超时、任何异常，一律回退 rule 排序，
meta 写 `degraded=True` + `reason`，**绝不把异常抛给调用方**。模型懒加载 + 进程内单例 + 锁，
所以第一次 cold call 可能因为 19s 加载超出 `RERANK_TIMEOUT_MS` 而降级，但加载线程会继续跑完，
下一次调用直接命中单例。
"""

from __future__ import annotations

import concurrent.futures
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings
from app.retrieval.hybrid import Hit
from app.retrieval.texts import Doc

MODES = ("off", "rule", "cross", "llm")

CE_THRESHOLD = 0.2  # cross-encoder 分数低于此值判「不相关」（spec §20.4）
CE_WEIGHT = 0.5  # 通过的候选：score = rule_score + 0.5 * ce_score
MIN_SEGMENT = 2  # 关键词片段长度下限，短于此的片段不参与覆盖率
REQUIRED_META_KEYS = (
    "mode",
    "requested",
    "n_in",
    "n_out",
    "ce_used",
    "ce_ms",
    "ce_load_ms",
    "dropped",
    "degraded",
    "reason",
    "llm_requested",
    "rule_top",
)

# --------------------------------------------------------------------------- #
# 单例（懒加载）
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()
_MODEL = None
_MODEL_KEY: str | None = None
_FAILED: dict[str, str] = {}


@dataclass(slots=True)
class RankedItem:
    """重排后的一条候选。`breakdown` 是给 trace 看的账本，`score` 是最终排序依据。"""

    doc: Doc
    rrf: float
    fts_rank: int | None = None
    vec_rank: int | None = None
    rule_score: float = 0.0
    ce_score: float | None = None
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "owner_table": self.doc.owner_table,
            "owner_id": self.doc.owner_id,
            "title": self.doc.title,
            "snippet": self.doc.snippet(),
            "meta": dict(self.doc.meta or {}),
            "rrf": round(float(self.rrf), 4),
            "fts_rank": self.fts_rank,
            "vec_rank": self.vec_rank,
            "rule_score": round(float(self.rule_score), 4),
            "ce_score": None if self.ce_score is None else round(float(self.ce_score), 4),
            "score": round(float(self.score), 4),
            "breakdown": _round_map(self.breakdown),
        }


# --------------------------------------------------------------------------- #
# 规则分（第一级）
# --------------------------------------------------------------------------- #
_SEGMENT_RE = re.compile(
    r"[\s\u3000,，、;；:：!！?？。.…·\-—_~～/\\|()（）\[\]【】{}｛｝<>《》「」『』\"'“”‘’*#+]+"
)


def segments(query: str) -> list[str]:
    """按空白与中英文标点切段，丢掉长度 < 2 的碎片（「尺码」保留，「表」丢弃）。"""
    return [seg for seg in _SEGMENT_RE.split(query or "") if len(seg) >= MIN_SEGMENT]


def _doc_text(doc: Doc) -> str:
    return "\n".join(part for part in (doc.title, doc.body) if part)


def keyword_coverage(query: str, doc: Doc) -> float:
    """query 片段命中 `title + body` 的比例；没有可用片段时为 0。"""
    parts = segments(query)
    if not parts:
        return 0.0
    text = _doc_text(doc)
    hits = sum(1 for part in parts if part in text)
    return hits / len(parts)


def rule_score(
    doc: Doc,
    *,
    root_causes=(),
    metrics=(),
    risk_max: int = 2,
    success_rate: float = 0.5,
    freshness: float = 0.0,
    query: str = "",
) -> tuple[float, dict]:
    """第一级规则分。返回 (total, breakdown)，breakdown 的 total 恒等于各项之和。

    `query` 只服务于 trigger（关键词覆盖率），作为 keyword-only 参数追加在末尾，
    不改变 spec 规定的其它参数与默认值。
    """
    meta = doc.meta or {}
    card_roots = {str(item) for item in (meta.get("root_causes") or ())}
    card_metrics = {str(item) for item in (meta.get("metrics") or ())}
    root_hits = len(card_roots & {str(item) for item in (root_causes or ())})
    metric_hits = len(card_metrics & {str(item) for item in (metrics or ())})

    coverage = keyword_coverage(query, doc)
    risk_value = _as_int(meta.get("risk"), 0)
    risk_max_value = _as_int(risk_max, 2)
    risk_penalty = -0.5 * (risk_value - risk_max_value) if risk_value > risk_max_value else 0.0

    breakdown = {
        "root": 3.0 * root_hits,
        "metric": 2.0 * metric_hits,
        "trigger": 1.5 * coverage,
        "success": 0.5 * _as_float(success_rate, 0.0),
        "risk": risk_penalty,
        "freshness": 0.3 * _as_float(freshness, 0.0),
    }
    total = sum(breakdown.values())
    breakdown["total"] = total
    return total, breakdown


# --------------------------------------------------------------------------- #
# 第二级：cross-encoder
# --------------------------------------------------------------------------- #
def _resolve_path(path: Path) -> Path | None:
    """兼容「模型目录」与「HF cache 根（含 snapshots/<hash>）」两种布局。"""
    if (path / "config.json").exists():
        return path
    snapshots = path / "snapshots"
    if snapshots.is_dir():
        for snap in sorted(snapshots.iterdir(), reverse=True):
            if (snap / "config.json").exists():
                return snap
    return None


def _pick_model_path(raw: object) -> tuple[Path | None, str]:
    """校验模型路径；失败时返回人话 reason（不抛异常）。"""
    text = str(raw or "").strip()
    if not text:
        return None, "ce_unavailable: rerank_model_path is empty"
    path = Path(text)
    if not path.exists():
        return None, f"ce_unavailable: model path not found: {path}"
    if not path.is_dir():
        return None, f"ce_unavailable: model path is not a directory: {path}"
    resolved = _resolve_path(path)
    if resolved is None:
        return None, f"ce_unavailable: no config.json under: {path}"
    return resolved, ""


def _load_ce(path: str):
    """懒加载 + 单例 + 锁。返回 (model, reason)，失败时不抛异常。"""
    global _MODEL, _MODEL_KEY
    with _LOCK:
        if _MODEL is not None and _MODEL_KEY == path:
            return _MODEL, ""
        if path in _FAILED:
            return None, _FAILED[path]
        # 离线优先：本机模型已在盘上，不让 HF 去联网探测
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        try:
            import torch
            from sentence_transformers import CrossEncoder

            torch.set_num_threads(min(8, os.cpu_count() or 4))
            model = CrossEncoder(path, device="cpu")
        except Exception as exc:  # 缺依赖 / 权重坏 / 显存不足……都走降级
            reason = f"ce_unavailable: {type(exc).__name__}: {exc}"
            _FAILED[path] = reason
            return None, reason
        _MODEL, _MODEL_KEY = model, path
        return _MODEL, ""


def _prob(value: float) -> float:
    """bge-reranker 的 predict 在部分版本返回 logits，这里统一成 0–1。"""
    if 0.0 <= value <= 1.0:
        return value
    clipped = max(-60.0, min(60.0, value))
    return 1.0 / (1.0 + math.exp(-clipped))


def _ce_predict(model, query: str, docs: list[Doc]) -> list[float]:
    pairs = [(query, _doc_text(doc)) for doc in docs]
    raw = model.predict(pairs, batch_size=8, show_progress_bar=False)
    try:
        values = [float(item) for item in raw.reshape(-1)]
    except AttributeError:
        values = [float(item) for item in raw]
    if len(values) != len(docs):
        raise ValueError(f"score count mismatch: {len(values)} != {len(docs)}")
    return [_prob(value) for value in values]


def _ce_run(path: Path, query: str, docs: list[Doc]) -> tuple[list[float] | None, float, float, str]:
    """在 worker 线程里跑「加载 + 打分」，分别计时，异常转成 reason。"""
    started = time.perf_counter()
    model, reason = _load_ce(str(path))
    load_ms = (time.perf_counter() - started) * 1000.0
    if model is None:
        return None, load_ms, 0.0, reason
    started = time.perf_counter()
    try:
        scores = _ce_predict(model, query, docs)
    except Exception as exc:
        return None, load_ms, (time.perf_counter() - started) * 1000.0, f"ce_unavailable: {type(exc).__name__}: {exc}"
    return scores, load_ms, (time.perf_counter() - started) * 1000.0, ""


def reset_cache() -> None:
    """测试用：清掉模型单例、失败缓存。"""
    global _MODEL, _MODEL_KEY
    with _LOCK:
        _MODEL = None
        _MODEL_KEY = None
        _FAILED.clear()


def ce_loaded() -> bool:
    """模型是否已在进程内（/api/health 的 rerank_ready 用得上）。"""
    return _MODEL is not None


# --------------------------------------------------------------------------- #
# 第三级：重排入口
# --------------------------------------------------------------------------- #
def rerank(
    query: str,
    items,
    *,
    root_causes=(),
    metrics=(),
    risk_max: int = 2,
    mode: str | None = None,
    top_n: int | None = None,
    timeout_ms: int | None = None,
    model_path: str | None = None,
) -> tuple[list[RankedItem], dict]:
    """对 `Hit` 列表做三级重排，返回 (top_n 条, meta)。任何失败都降级为 rule，不抛异常。"""
    settings = get_settings()
    text = (query or "").strip()
    candidates = list(items or ())

    requested = str(mode or getattr(settings, "rerank_mode", "rule") or "rule").strip().lower()
    limit = _positive_int(top_n, _positive_int(getattr(settings, "rerank_top_n", 6), 6))
    budget_ms = _positive_int(timeout_ms, _positive_int(getattr(settings, "rerank_timeout_ms", 3500), 3500))

    effective = "cross" if requested == "llm" else requested
    meta: dict = {
        "mode": effective if effective in MODES else "rule",
        "requested": requested,
        "n_in": len(candidates),
        "n_out": 0,
        "ce_used": False,
        "ce_ms": 0.0,
        "ce_load_ms": 0.0,
        "dropped": 0,
        "degraded": False,
        "reason": "",
        "llm_requested": requested == "llm",
        "rule_top": [],
    }

    if effective not in MODES:  # 拼错的档位按 rule 处理，别让配置错误变成 500
        effective = "rule"
        meta["mode"] = "rule"
        meta["degraded"] = True
        meta["reason"] = f"unknown_mode: {requested}"

    if not candidates:
        return [], meta

    if effective == "off":
        # 只按 rrf 降序：不碰规则分，也不碰模型
        ranked = [
            RankedItem(
                doc=_doc_of(item),
                rrf=_as_float(getattr(item, "rrf", 0.0), 0.0),
                fts_rank=getattr(item, "fts_rank", None),
                vec_rank=getattr(item, "vec_rank", None),
                rule_score=0.0,
                ce_score=None,
                score=_as_float(getattr(item, "rrf", 0.0), 0.0),
                breakdown={},
            )
            for item in candidates
        ]
        ranked.sort(key=_order_key)
        out = ranked[:limit]
        meta["n_out"] = len(out)
        return out, meta

    ranked = [
        _rank_one(item, text, root_causes=root_causes, metrics=metrics, risk_max=risk_max)
        for item in candidates
    ]
    ranked.sort(key=_order_key)
    meta["rule_top"] = [_key(item.doc) for item in ranked[:limit]]

    if effective == "rule":
        out = ranked[:limit]
        meta["n_out"] = len(out)
        return out, meta

    # ---- cross / llm ----
    shortlist = ranked[:limit]
    path, reason = _pick_model_path(model_path or getattr(settings, "rerank_model_path", ""))
    if path is None:
        return _degrade(shortlist, meta, reason)

    timeout_s = budget_ms / 1000.0
    scores: list[float] | None = None
    load_ms = 0.0
    score_ms = 0.0
    failure = ""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank-ce")
    try:
        future = executor.submit(_ce_run, path, text, [item.doc for item in shortlist])
        try:
            scores, load_ms, score_ms, failure = future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            # 不等了：加载线程会继续跑完并写进单例，下一次调用直接命中
            scores, failure = None, f"ce_timeout: {budget_ms}ms budget exceeded"
            score_ms = float(budget_ms)
        except Exception as exc:
            scores, failure = None, f"ce_unavailable: {type(exc).__name__}: {exc}"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    meta["ce_load_ms"] = float(load_ms)
    meta["ce_ms"] = float(score_ms)
    if scores is None:
        return _degrade(shortlist, meta, failure or "ce_unavailable: no scores")

    meta["ce_used"] = True
    kept: list[RankedItem] = []
    for item, ce_score in zip(shortlist, scores):
        if ce_score < CE_THRESHOLD:
            continue
        item.ce_score = ce_score
        item.score = item.rule_score + CE_WEIGHT * ce_score
        item.breakdown["ce"] = CE_WEIGHT * ce_score
        item.breakdown["total"] = item.score
        kept.append(item)
    meta["dropped"] = len(shortlist) - len(kept)
    if not kept:
        meta["reason"] = f"ce_all_below_threshold: {len(shortlist)} candidates < {CE_THRESHOLD}"
    kept.sort(key=_order_key)
    out = kept[:limit]
    meta["n_out"] = len(out)
    return out, meta


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _rank_one(item, query: str, *, root_causes, metrics, risk_max: int) -> RankedItem:
    doc = _doc_of(item)
    total, breakdown = rule_score(
        doc,
        root_causes=root_causes,
        metrics=metrics,
        risk_max=risk_max,
        query=query,
    )
    return RankedItem(
        doc=doc,
        rrf=_as_float(getattr(item, "rrf", 0.0), 0.0),
        fts_rank=getattr(item, "fts_rank", None),
        vec_rank=getattr(item, "vec_rank", None),
        rule_score=total,
        ce_score=None,
        score=total,
        breakdown=breakdown,
    )


def _degrade(out: list[RankedItem], meta: dict, reason: str) -> tuple[list[RankedItem], dict]:
    """降级回 rule：meta 如实记录，结果仍是规则序的 top_n（可能为空但不会抛错）。"""
    meta["mode"] = "rule"
    meta["degraded"] = True
    meta["reason"] = reason
    meta["n_out"] = len(out)
    return list(out), meta


def _doc_of(item) -> Doc:
    return item.doc if hasattr(item, "doc") else item


def _order_key(item: RankedItem) -> tuple[float, float]:
    return (-item.score, -item.rrf)


def _key(doc: Doc) -> str:
    return f"{doc.owner_table}:{doc.owner_id}"


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _positive_int(value: object, default: int) -> int:
    number = _as_int(value, default)
    return number if number > 0 else default


def _round_map(values: dict | None) -> dict:
    out: dict = {}
    for key, value in (values or {}).items():
        if value is None:
            out[key] = None
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = round(float(value), 4)
        else:
            out[key] = value
    return out


__all__ = [
    "Hit",
    "MODES",
    "RankedItem",
    "ce_loaded",
    "keyword_coverage",
    "rerank",
    "reset_cache",
    "rule_score",
    "segments",
]
