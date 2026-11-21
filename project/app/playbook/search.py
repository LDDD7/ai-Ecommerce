"""策略库检索：混合召回 → 三级重排 → 风险硬过滤 → 可执行的 `SearchHit`。

分两层：

- `search_playbook`：自然语言 query 走 FTS5 + 向量混合召回（`app.retrieval.hybrid`），
  再交给 `app.retrieval.rerank.rerank` 做规则 / cross-encoder / LLM 重排；
- `search_for_codes`：诊断卡已经给出根因码（R1..R10）时跳过召回，直接按根因索引取卡，
  快且不会因为 query 措辞跑偏。

降级原则（spec §20.4）：重排只能锦上添花，不能成为单点故障。`rerank` 模块未落地或
调用抛错时退回「RRF 降序」，并在 `meta` 里写 `degraded=True` 与原因，绝不把异常抛给上层。

返回值尽量「拿起来就能用」：`SearchHit.as_dict()` 只带执行需要的信息，不塞 `search_text`
（动辄几百字，进 trace / SSE 都嫌重），需要时用 `hit.card` 取完整卡。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.playbook.loader import by_root, load_cards
from app.playbook.schema import ROOT_CAUSES, Card
from app.retrieval.hybrid import search as hybrid_search
from app.retrieval.texts import OWNER_PLAYBOOK

try:
    from app.retrieval.rerank import rerank  # noqa: F401
except Exception:  # pragma: no cover - 重排模块可能尚未就绪
    rerank = None

# 召回候选数：27 张卡里取 12 条，足够覆盖多义 query，又不至于让重排白跑。
RECALL_LIMIT = 12
# 返回条数上限：策略卡是「可执行建议」，一次给 20 张以上只会让人挑花眼。
MAX_LIMIT = 20
# 一张卡最多给 4 条理由：再多就不是解释，是噪音。
MAX_REASONS = 4
# spec §8/§20.4 约定的兜底话术，评测里按这句话判定「可执行性」。
EMPTY_NOTE = "策略库无匹配卡，建议人工介入"

ACTION_NAMES: dict[str, str] = {
    "adjust_price": "调价",
    "adjust_bid": "调出价",
    "swap_creative": "换素材",
    "issue_coupon": "发券",
    "restock_alert": "补货预警",
}


@dataclass(slots=True)
class SearchHit:
    """一条策略卡检索结果。

    `card` 保留整张卡（`steps` / `preconditions` 执行时要用），`as_dict()` 才做裁剪。
    """

    card: Card
    score: float
    rule_score: float
    ce_score: float | None
    rrf: float
    risk: int
    action_type: str
    reasons: list[str]
    expected_effect: dict

    def as_dict(self) -> dict:
        """给 API / trace 用的紧凑视图；刻意不带 `search_text`（太长）。"""
        return {
            "card_id": self.card.card_id,
            "title": self.card.title,
            "risk": self.risk,
            "action_type": self.action_type,
            "scope": list(self.card.scope),
            "root_causes": list(self.card.root_causes),
            "metrics": list(self.card.metrics),
            "score": round(float(self.score), 4),
            "rule_score": round(float(self.rule_score), 4),
            "ce_score": None if self.ce_score is None else round(float(self.ce_score), 4),
            "rrf": round(float(self.rrf), 6),
            "reasons": list(self.reasons),
            "expected_effect": dict(self.expected_effect),
            "success_metric": self.card.success_metric,
            "preconditions": list(self.card.preconditions),
            "steps": list(self.card.steps),
        }


@dataclass(slots=True)
class _Ranked:
    """重排结果的内部统一视图，兼容 RankedItem / 降级时的 hybrid.Hit。"""

    doc: Any
    rrf: float = 0.0
    fts_rank: int | None = None
    vec_rank: int | None = None
    rule_score: float = 0.0
    ce_score: float | None = None
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)


# ---- 小工具 ----


def _normalize_codes(codes: str | Iterable[str] | None) -> list[str]:
    """根因码统一成大写、去空格、去重且保留传入顺序（R10 不拆成 R1+R0）。"""
    if codes is None:
        return []
    if isinstance(codes, str):
        raw: list[str] = [codes]
    else:
        raw = [str(code) for code in codes if code is not None]
    out: list[str] = []
    for code in raw:
        text = str(code).strip().upper()
        if text and text not in out:
            out.append(text)
    return out


def _normalize_metrics(metrics: str | Iterable[str] | None) -> list[str]:
    if metrics is None:
        return []
    raw = [metrics] if isinstance(metrics, str) else [str(m) for m in metrics if m is not None]
    out: list[str] = []
    for metric in raw:
        text = str(metric).strip()
        if text and text not in out:
            out.append(text)
    return out


def _clamp_limit(limit: int) -> tuple[int, bool]:
    """把 limit 夹到 1..20，并告诉调用方是否夹过（调用方可能写了 0 或 100）。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 5
    clamped = value < 1 or value > MAX_LIMIT
    return min(max(value, 1), MAX_LIMIT), clamped


def _expected_effect(card: Card) -> dict:
    effect = card.expected_effect
    return {
        "metric": effect.metric,
        "direction": effect.direction,
        "range": [effect.range[0], effect.range[1]],
        "window_days": effect.window_days,
    }


def _build_reasons(
    card: Card,
    roots: Iterable[str],
    metrics: Iterable[str],
    fts_rank: int | None,
    vec_rank: int | None,
) -> list[str]:
    """人读的命中理由：根因 > 动作 > 指标 > 来源名次，最多 4 条。

    顺序是有意的：根因是「为什么是这张卡」，动作是「它要干什么」，指标是「能改善什么」，
    名次只是来源注释，宁可被截掉也不能抢占前三条。
    """
    root_set = {str(code).strip().upper() for code in roots}
    metric_set = {str(metric).strip().lower() for metric in metrics}

    reasons: list[str] = []
    for code in card.root_causes:
        if code in root_set:
            reasons.append(f"命中根因 {code} {ROOT_CAUSES[code]}")
    reasons.append(f"动作：{ACTION_NAMES.get(card.action_type, card.action_type)}")
    for metric in card.metrics:
        if metric.lower() in metric_set:
            reasons.append(f"改善 {metric}")

    ranks: list[str] = []
    if fts_rank:
        ranks.append(f"fts#{fts_rank}")
    if vec_rank:
        ranks.append(f"vec#{vec_rank}")
    if ranks:
        reasons.append("rrf 名次 " + " ".join(ranks))

    unique: list[str] = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    return unique[:MAX_REASONS]


def _fallback_rank(hits: list[Any]) -> list[_Ranked]:
    """重排不可用时的降级：纯按 RRF 降序（RRF 已融合两路名次，本身就够稳）。"""
    ranked = [
        _Ranked(
            doc=hit.doc,
            rrf=float(getattr(hit, "rrf", 0.0) or 0.0),
            fts_rank=getattr(hit, "fts_rank", None),
            vec_rank=getattr(hit, "vec_rank", None),
            rule_score=0.0,
            ce_score=None,
            score=float(getattr(hit, "rrf", 0.0) or 0.0),
        )
        for hit in hits
    ]
    ranked.sort(key=lambda item: item.rrf, reverse=True)
    return ranked


def _coerce_ranked(item: Any) -> _Ranked:
    """把 rerank 的 RankedItem 摊成内部视图；字段缺失时给出保守默认值。"""
    rrf = float(getattr(item, "rrf", 0.0) or 0.0)
    raw_score = getattr(item, "score", None)
    score = rrf if raw_score is None else float(raw_score)
    return _Ranked(
        doc=getattr(item, "doc", None),
        rrf=rrf,
        fts_rank=getattr(item, "fts_rank", None),
        vec_rank=getattr(item, "vec_rank", None),
        rule_score=float(getattr(item, "rule_score", 0.0) or 0.0),
        ce_score=getattr(item, "ce_score", None),
        score=score,
        breakdown=getattr(item, "breakdown", None) or {},
    )


def _cards_by_id() -> dict[str, Card]:
    return {card.card_id: card for card in load_cards()}


def _hit_from_ranked(
    item: _Ranked,
    card: Card,
    roots: Iterable[str],
    metrics: Iterable[str],
) -> SearchHit:
    return SearchHit(
        card=card,
        score=item.score,
        rule_score=item.rule_score,
        ce_score=item.ce_score,
        rrf=item.rrf,
        risk=card.risk,
        action_type=card.action_type,
        reasons=_build_reasons(card, roots, metrics, item.fts_rank, item.vec_rank),
        expected_effect=_expected_effect(card),
    )


# ---- 对外两个入口 ----


def search_playbook(
    db_path: str,
    query: str,
    *,
    root_causes: Iterable[str] = (),
    metrics: Iterable[str] = (),
    risk_max: int = 2,
    limit: int = 5,
    mode: str | None = None,
    with_vec: bool | None = None,
) -> tuple[list[SearchHit], dict]:
    """自然语言检索策略卡。

    Args:
        db_path: SQLite 库路径（策略卡本体来自 YAML，这里只用于 FTS / 向量索引）。
        query: 诊断证据拼成的检索语句。
        root_causes: 已知根因码，命中会在 reasons 里点名（也是规则重排的强特征）。
        metrics: 需要改善的指标名，命中同样进 reasons。
        risk_max: 风险上限（0 只出无需审批的卡）。
        limit: 返回条数，越界自动夹到 1..20。
        mode: 重排模式透传（rule / cross / llm），None 交给 rerank 决定。
        with_vec: 是否用向量召回；None 交给检索层按环境决定。消融实验用它关掉向量。

    Returns:
        (hits, meta)。无召回或无匹配时 `hits==[]` 且 `meta["note"]` 给出人工兜底提示。
    """
    q = (query or "").strip()
    roots = _normalize_codes(root_causes)
    metric_list = _normalize_metrics(metrics)
    limit, clamped = _clamp_limit(limit)
    risk_max = int(risk_max)

    meta: dict = {
        "query": q,
        "root_causes": list(roots),
        "metrics": list(metric_list),
        "risk_max": risk_max,
        "recall": 0,
        "returned": 0,
        "filtered_risk": 0,
        "fts": 0,
        "vec": 0,
        "mode": mode,
        "degraded": False,
        "reason": None,
        "note": "",
        "clamped": clamped,
    }

    if not q:
        meta["note"] = EMPTY_NOTE
        return [], meta

    hits, base_meta = hybrid_search(
        db_path,
        q,
        owner_table=OWNER_PLAYBOOK,
        limit=RECALL_LIMIT,
        with_vec=with_vec,
    )
    # 双保险：owner_table 已经限定了 playbook，但检索层若被复用/改错，这里不能放过 policy 文档。
    candidates = [
        hit
        for hit in hits
        if getattr(getattr(hit, "doc", None), "owner_table", None) == OWNER_PLAYBOOK
    ]
    meta["recall"] = len(candidates)
    meta["fts"] = int(
        base_meta.get("fts") or sum(1 for hit in candidates if getattr(hit, "fts_rank", None))
    )
    meta["vec"] = int(
        base_meta.get("vec") or sum(1 for hit in candidates if getattr(hit, "vec_rank", None))
    )

    if not candidates:
        meta["note"] = EMPTY_NOTE
        return [], meta

    ranked: list[_Ranked]
    if rerank is None:  # 重排模块还没落地：退 RRF
        ranked = _fallback_rank(candidates)
        meta["degraded"] = True
        meta["reason"] = "rerank_unavailable"
    else:
        try:
            # 显式给 top_n，否则 rerank 会按 RERANK_TOP_N（默认 6）截断，limit>6 就永远拿不满。
            # 不传 risk_max：风险是硬过滤，留在这里做才能精确统计 filtered_risk。
            result = rerank(
                q,
                candidates,
                root_causes=roots,
                metrics=metric_list,
                mode=mode,
                top_n=min(RECALL_LIMIT, limit),
            )
            raw_ranked, rerank_meta = result
            ranked = [_coerce_ranked(item) for item in raw_ranked]
            if isinstance(rerank_meta, dict):
                if rerank_meta.get("mode"):
                    meta["mode"] = rerank_meta["mode"]
                # cross / llm 档失败会自己降级回 rule，这个「静默降级」必须透传，
                # 否则上层以为拿到了精排结果。
                if rerank_meta.get("degraded") and not meta["degraded"]:
                    meta["degraded"] = True
                    meta["reason"] = rerank_meta.get("reason") or "rerank_degraded"
        except Exception as exc:  # 重排不能成为单点故障
            ranked = _fallback_rank(candidates)
            meta["degraded"] = True
            meta["reason"] = f"rerank_failed: {exc}"

    card_by_id = _cards_by_id()
    out: list[SearchHit] = []
    for item in ranked:
        card = card_by_id.get(getattr(item.doc, "owner_id", None))
        if card is None:  # 索引里可能有旧卡 / 非本目录卡，跳过而不是崩
            continue
        if card.risk > risk_max:
            meta["filtered_risk"] += 1
            continue
        out.append(_hit_from_ranked(item, card, roots, metric_list))

    # rerank 理论上已排好序，但降级路径和假 rerank 未必；统一按 score 再排一次，平局看 rrf。
    out.sort(key=lambda hit: (-hit.score, -hit.rrf, hit.card.card_id))
    out = out[:limit]

    meta["returned"] = len(out)
    if not out:
        meta["note"] = EMPTY_NOTE
        if meta["filtered_risk"]:
            meta["note"] += "（候选卡均高于风险上限）"
    return out, meta


def search_for_codes(
    db_path: str | None,
    codes: str | Iterable[str] | None = None,
    *,
    risk_max: int = 2,
    limit: int = 5,
) -> tuple[list[SearchHit], dict]:
    """按根因码直达策略卡（诊断卡拿到 R4 之后不必再猜 query 措辞）。

    `db_path` 只为与 `search_playbook` 保持同构签名——策略卡来自 YAML，不查库。
    也接受单参写法 `search_for_codes("R4")`（此时第一参当作 codes）。

    未知码（如拼错的 R99）不抛异常，记进 `meta["unknown_codes"]`，方便诊断层提示。
    """
    if codes is None:
        # 兼容 search_for_codes("R4")：第一个位置参数其实是 codes。
        db_path, codes = None, db_path

    requested = _normalize_codes(codes)
    limit, clamped = _clamp_limit(limit)
    risk_max = int(risk_max)

    meta: dict = {
        "engine": "by_root",
        "codes": list(requested),
        "risk_max": risk_max,
        "returned": 0,
        "filtered_risk": 0,
        "unknown_codes": [code for code in requested if code not in ROOT_CAUSES],
        "degraded": False,
        "reason": None,
        "note": "",
        "clamped": clamped,
    }

    known = [code for code in requested if code in ROOT_CAUSES]
    if not known:
        meta["note"] = EMPTY_NOTE
        return [], meta

    index = by_root(load_cards())
    wanted = set(known)
    seen: dict[str, Card] = {}
    for code in known:
        for card in index.get(code, []):
            seen[card.card_id] = card

    scored: list[tuple[int, Card]] = []
    for card in seen.values():
        matched = sum(1 for code in card.root_causes if code in wanted)
        if matched:
            scored.append((matched, card))

    # 命中码多的优先（R3+R7 的双根因卡更贴切），同分先出低风险卡，再按 card_id 稳定排序。
    scored.sort(key=lambda pair: (-pair[0], pair[1].risk, pair[1].card_id))

    out: list[SearchHit] = []
    for matched, card in scored:
        if card.risk > risk_max:
            meta["filtered_risk"] += 1
            continue
        out.append(
            SearchHit(
                card=card,
                score=float(matched),
                rule_score=float(matched),
                ce_score=None,
                rrf=0.0,
                risk=card.risk,
                action_type=card.action_type,
                reasons=_build_reasons(card, wanted, (), None, None),
                expected_effect=_expected_effect(card),
            )
        )

    out = out[:limit]
    meta["returned"] = len(out)
    if not out:
        meta["note"] = EMPTY_NOTE
        if meta["filtered_risk"]:
            meta["note"] += "（候选卡均高于风险上限）"
    return out, meta
