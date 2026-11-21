"""诊断卡：把「异常 → 分解 → 贡献 → 漏斗 → 证据」拼成能给人看的结论。

一张卡 = 一个「维度值 × 主症状 × 最可能的根因」，字段和 spec §11.1 对齐：
结论、证据 chip、影响金额、置信度、窗口、注意事项。前端拿到 JSON 就能渲染，
不需要再算任何东西。

硬规则（spec §7.5）：**证据不全就不许出卡**。如果某一组异常拿不出完整证据，
这张卡会被丢掉并记进 `meta.dropped`，而不是硬写一句话糊过去。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.diagnose.anomaly import AnomalyPoint, DetectionResult, detect
from app.core.config import get_settings
from app.diagnose.contribution import ContributionResult, contribution
from app.diagnose.critic import apply_verdicts, critique
from app.diagnose.decompose import Decomposition, describe as describe_decomposition, gmv_decompose
from app.diagnose.evidence import Evidence, complete_only, fmt_num, is_complete
from app.diagnose.funnel import Funnel, describe as describe_funnel, funnel
from app.diagnose.hypothesis import Candidate, competitor_gap, rank, refund_split
from app.diagnose.recheck import recheck_many
from app.diagnose.replan import (
    ACTION_DEMOTE,
    ACTION_DROP,
    ACTION_SKIP,
    ACTION_UPGRADE,
    REQUIRED_SUPPORT,
    plan_recovery,
)
from app.insights.overview import DIM_LABELS, _query, resolve_as_of

MAX_EVIDENCE = 4
MAX_CARDS = 5
MAX_PER_CODE = 2

# 第二轮 replan：只救「已有 scope」的卡，把窗口从 7 天放到 14 天找辅证。
# 预算 2.5s——一次 detect 约 0.7–1.0s，超预算就不做，宁可少一轮也不能拖垮 8s。
REPLAN_DAYS = 14
REPLAN_BUDGET_MS = 2_500
# 补到辅证后的置信度下限：只证明「证据更完整了」，不等于直接判成立。
REPLAN_UPGRADE_CONF = 0.60
# 降权系数：留一条记录，但让它排到后面。
REPLAN_DEMOTE_FACTOR = 0.75

# 诊断只关心「变坏」的方向：上涨才是好事的指标 vs 上涨就是坏事的指标。
# 异常检测本身两个方向都报（趋势页要用），但出卡必须按方向过滤，
# 否则会把「ROI 涨了 17%」写成「投放 ROI 恶化」。
GOOD_UP = {
    "gmv",
    "paid_orders",
    "pay_buyers",
    "uv",
    "pv",
    "impressions",
    "clicks",
    "ctr",
    "cvr",
    "detail_uv",
    "add_cart_uv",
    "add_cart_rate",
    "aov",
    "unit_price",
    "gross_margin",
    "roi",
    "dsr",
}
GOOD_DOWN = {"refund_rate", "return_rate", "neg_review_rate", "stockout_rate", "ad_spend", "cpc"}
PROMO_CAVEAT_SKEW = 0.20


def is_bad(point: AnomalyPoint) -> bool:
    """这个异常点是不是「变坏」的方向。"""
    delta = point.delta
    if delta is None:
        return False
    if point.metric in GOOD_UP:
        return delta < 0
    if point.metric in GOOD_DOWN:
        return delta > 0
    return True


@dataclass
class DiagnosisCard:
    card_id: str
    code: str
    title: str
    headline: str
    scope: dict[str, Any]
    severity: str
    confidence: float
    primary_metric: str
    window: dict[str, str]
    baseline_window: dict[str, str]
    evidence: list[Evidence] = field(default_factory=list)
    gmv_impact: float | None = None
    impact_share: float | None = None
    competitor_gap: float | None = None
    narrative: str = ""
    caveats: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "code": self.code,
            "title": self.title,
            "headline": self.headline,
            "scope": self.scope,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "primary_metric": self.primary_metric,
            "window": self.window,
            "baseline_window": self.baseline_window,
            "evidence": [e.as_dict() for e in self.evidence],
            "gmv_impact": _r(self.gmv_impact, 2),
            "impact_share": _r(self.impact_share, 4),
            "competitor_gap": _r(self.competitor_gap, 4),
            "narrative": self.narrative,
            "caveats": self.caveats,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def _window_dict(start: date, end: date) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


ALL_DIM = "all"


def _group_points(points: list[AnomalyPoint]) -> dict[tuple[str, str], list[AnomalyPoint]]:
    """按 (维度, 维度值) 分组；全店级别的点（detect 里 dimension=None）归一成 `all`。"""
    groups: dict[tuple[str, str], list[AnomalyPoint]] = {}
    for p in points:
        dim = p.dimension or ALL_DIM
        val = "全店" if p.dim_value is None else str(p.dim_value)
        groups.setdefault((dim, val), []).append(p)
    return groups


def _impact_for(
    db_path: str | Path,
    dimension: str,
    dim_value: str,
    cur: tuple[date, date],
    prev: tuple[date, date],
    cache: dict[str, ContributionResult],
) -> tuple[float | None, float | None]:
    """该维度值对成交额变化的贡献（金额、占比）。按维度缓存，避免重复查库。"""
    if dimension == ALL_DIM:
        cur_gmv = _total_gmv(db_path, cur)
        prev_gmv = _total_gmv(db_path, prev)
        if cur_gmv is None or prev_gmv is None:
            return None, None
        delta = cur_gmv - prev_gmv
        return delta, 1.0

    if dimension not in cache:
        cache[dimension] = contribution(db_path, "gmv", dimension, cur, prev)
    res = cache[dimension]
    for part in res.parts:
        if part.dim_value == dim_value:
            return part.delta, part.share
    return None, None


def _total_gmv(db_path: str | Path, window: tuple[date, date]) -> float | None:
    cols, rows = _query(db_path, ["gmv"], window[0], window[1])
    if not rows:
        return None
    rec = dict(zip(cols, rows[0]))
    value = rec.get("gmv")
    return None if value is None else float(value)


def _narrative(
    point: AnomalyPoint,
    cand: Candidate,
    evidence: list[Evidence],
    gmv_impact: float | None,
    impact_share: float | None,
    dim_label: str,
) -> str:
    scope_txt = "全店" if dim_label == "全店" else f"{dim_label}「{point.dim_value}」"
    head = (
        f"{point.window.start}~{point.window.end}，{scope_txt}"
        f"{point.display} {fmt_num(point.baseline, point.unit)} → {fmt_num(point.current, point.unit)}"
        f"（{_pct(point.delta_pct)}，z={point.z:.1f}）"
    )
    support = [e for e in evidence if e.metric != point.metric]
    if support:
        extra = "，同期 " + "、".join(f"{e.display} {_pct(e.delta_pct)}" for e in support[:2])
    else:
        extra = ""
    body = f"；{cand.why}" if cand.why else ""
    impact = ""
    if gmv_impact is not None:
        word = "降幅" if gmv_impact < 0 else "增幅"
        share = f"，占整体{word} {abs(impact_share) * 100:.0f}%" if impact_share is not None else ""
        impact = f"。影响成交额 {gmv_impact:+,.0f} 元{share}"
    return f"{head}{extra}{body}{impact}。"


def _make_card(
    db_path: str | Path,
    group: list[AnomalyPoint],
    result: DetectionResult,
    cur: tuple[date, date],
    prev: tuple[date, date],
    cache: dict[str, ContributionResult],
    index: int,
) -> DiagnosisCard | None:
    primary = max(group, key=lambda p: p.score)
    dim = primary.dimension or ALL_DIM
    dim_value = "全店" if primary.dim_value is None else str(primary.dim_value)
    dim_label = DIM_LABELS.get(dim, "全店")

    gap = None
    if dim == "sku_id":
        g = competitor_gap(db_path, dim_value, cur)
        gap = g["gap"] if g else None

    cands = rank(
        primary.metric,
        z=primary.z,
        support=len(group),
        promo_skew=(result.meta.get("promo_share") or {}).get("skew"),
        gap=gap,
        split=(
            refund_split(db_path, dim if dim != ALL_DIM else None, dim_value, cur, prev)
            if primary.metric == "refund_rate"
            else None
        ),
        limit=3,
    )
    if not cands:
        return None
    top = cands[0]

    gmv_impact, impact_share = _impact_for(db_path, dim, dim_value, cur, prev, cache)

    evidence: list[Evidence] = []
    for p in sorted(group, key=lambda x: -x.score)[:MAX_EVIDENCE]:
        ev = Evidence.from_point(p, contribution_pct=impact_share if p is primary else None)
        if is_complete(ev):
            evidence.append(ev)
    if not evidence:
        return None

    caveats = []
    skew = (result.meta.get("promo_share") or {}).get("skew") or 0.0
    if result.meta.get("promo_dominated"):
        caveats.append("本期整段都在促销活动内，基线几乎没有促销天，单周同比不可比，建议按促销期整体看")
    if "promo_skew" in (primary.flags or []) and abs(skew) > PROMO_CAVEAT_SKEW:
        caveats.append(f"窗口内促销日占比变化 {skew:+.2f}，量价对比需谨慎")
    if gap is not None and gap >= 0.05:
        caveats.append(f"我方售价比竞品最低价高 {gap * 100:.1f}%")

    return DiagnosisCard(
        card_id=f"card_{index:03d}",
        code=top.code,
        title=top.title,
        headline=f"{dim_label}「{dim_value}」{primary.display} {_pct(primary.delta_pct)}",
        scope={"dim": dim, "dim_label": dim_label, "dim_value": dim_value},
        severity=primary.severity,
        confidence=top.confidence,
        primary_metric=primary.metric,
        window=_window_dict(primary.window.start, primary.window.end),
        baseline_window=_window_dict(primary.baseline_window.start, primary.baseline_window.end),
        evidence=evidence,
        gmv_impact=gmv_impact,
        impact_share=impact_share,
        competitor_gap=gap,
        narrative=_narrative(primary, top, evidence, gmv_impact, impact_share, dim_label),
        caveats=caveats,
        candidates=cands,
    )


def _attach_material(db_path: str | Path, cards: list[dict[str, Any]]) -> dict[str, Any]:
    """给所有 SKU 级诊断卡补素材发现；素材层坏了就整体跳过，不影响诊断。"""
    targets = [c for c in cards if (c.get("scope") or {}).get("dim") == "sku_id"]
    if not targets:
        return {"attached": 0, "targets": 0, "reason": "没有 SKU 级诊断卡"}

    try:
        from app.vision import inspector, material
    except Exception as exc:  # noqa: BLE001 - 模块缺失/依赖不全时不影响主流程
        return {
            "attached": 0,
            "targets": len(targets),
            "degraded": True,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    attached = 0
    degraded = False
    reasons: list[str] = []
    for card in targets:
        sku_id = card["scope"]["dim_value"]
        try:
            # 诊断路径默认走离线真值（mock）：一次诊断不该为了几张缩略图去发 3 次视觉请求。
            features, meta = inspector.inspect(db_path, sku_id, "main", mode="mock")
        except Exception as exc:  # noqa: BLE001
            degraded = True
            reasons.append(f"{sku_id}: {type(exc).__name__}")
            continue
        feats = features.as_dict()
        try:
            findings = material.findings(feats)
            score = material.material_score(feats)
        except Exception:  # noqa: BLE001 - 素材发现是加分项
            findings = [f.model_dump() for f in features.issues()]
            score = None
        card["material"] = {
            "score": score,
            "findings": findings,
            "evidence": features.to_evidence(),
            "compare_summary": "",
            "source": feats.get("source"),
            "degraded": bool(meta.get("degraded")),
        }
        attached += 1

    out: dict[str, Any] = {"attached": attached, "degraded": degraded, "targets": len(targets)}
    if reasons:
        out["reasons"] = reasons[:3]
    return out


# ---------------------------------------------------------------- 第二轮 replan


def _replan_scopes(
    cards: list[dict[str, Any]], verdicts: list[dict[str, Any]]
) -> dict[tuple[str, str], str]:
    """收集要复检的 scope：只挑「被削弱/驳回 + 有必需辅证 + 有 scope」的卡。

    没有必需辅证的根因（R1/R2/R7/R8）不需要复检：按 replan 规则 5 它们只会降权，
    多跑一次 detect 纯属浪费预算。
    """
    by_id = {v.get("card_id"): v for v in verdicts if v.get("card_id")}
    out: dict[tuple[str, str], str] = {}
    for card in cards:
        verdict = by_id.get(card.get("card_id"))
        if verdict is None or verdict.get("verdict") == "support":
            continue
        metric = REQUIRED_SUPPORT.get(str(card.get("code") or ""))
        scope = card.get("scope") or {}
        dim, value = scope.get("dim"), scope.get("dim_value")
        if not metric or not dim or not value:
            continue
        out[(str(dim), str(value))] = metric
    return out


def _run_replan(
    db_path: str | Path,
    cards: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
    as_of: date,
    dropped: list[dict[str, Any]],
    *,
    days: int = REPLAN_DAYS,
    budget_ms: int = REPLAN_BUDGET_MS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """对 Critic 削弱/驳回的卡做第二轮：同 scope 扩窗复检 → upgrade/drop/demote/skip。

    安全性：复检任何异常都只降级（`degraded=True`），卡片原样返回——第二轮是
    加分项，不能因为它挂了就不出诊断。
    """
    meta: dict[str, Any] = {
        "enabled": True,
        "days": days,
        "checked": 0,
        "upgrade": 0,
        "drop": 0,
        "demote": 0,
        "skip": 0,
        "items": [],
        "elapsed_ms": 0.0,
        "degraded": False,
    }
    if not cards:
        return cards, meta

    t0 = time.perf_counter()
    wanted = _replan_scopes(cards, verdicts)

    rechecks: dict[tuple[str, str], dict[str, Any]] = {}
    if wanted:
        if budget_ms is not None and (time.perf_counter() - t0) * 1000 > budget_ms:
            meta.update(degraded=True, reason=f"已用 {(time.perf_counter() - t0) * 1000:.0f}ms，超出复检预算")
            meta["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return cards, meta
        try:
            raw = recheck_many(
                db_path,
                list(wanted),
                as_of=as_of,
                required=wanted,
                days=days,
                cache={},
            )
            rechecks = {key: item.as_dict() for key, item in raw.items()}
        except Exception as exc:  # noqa: BLE001 - 复检是加分项，坏了就跳过
            meta.update(degraded=True, reason=f"{type(exc).__name__}: {exc}")
            meta["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return cards, meta

    try:
        recoveries = plan_recovery(cards, verdicts, rechecks)
    except Exception as exc:  # noqa: BLE001
        meta.update(degraded=True, reason=f"{type(exc).__name__}: {exc}")
        meta["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return cards, meta

    by_id = {card.get("card_id"): card for card in cards}
    dropped_ids: set[str] = set()
    for rec in recoveries:
        card = by_id.get(rec.card_id)
        if card is None:
            continue
        meta["checked"] += 1
        meta[rec.action] = meta.get(rec.action, 0) + 1
        meta["items"].append(
            {
                "card_id": rec.card_id,
                "code": rec.code,
                "action": rec.action,
                "reason": rec.reason,
            }
        )
        note = {"action": rec.action, "reason": rec.reason, "days": days}
        card["replan"] = note
        if rec.action == ACTION_UPGRADE:
            before = float(card.get("confidence") or 0.0)
            after = round(max(before, REPLAN_UPGRADE_CONF), 3)
            card["confidence"] = after
            critic = card.setdefault("critic", {})
            critic["verdict"] = "support"
            critic["confidence_before"] = round(before, 3)
            critic["confidence_after"] = after
            critic.setdefault("objections", [])
            card.setdefault("caveats", [])
            line = f"复审：{days} 天窗口补到 {rec.required_metric} 辅证，置信度上调"
            if line not in card["caveats"]:
                card["caveats"].append(line)
        elif rec.action == ACTION_DEMOTE:
            before = float(card.get("confidence") or 0.0)
            card["confidence"] = round(max(0.2, before * REPLAN_DEMOTE_FACTOR), 3)
        elif rec.action == ACTION_DROP:
            dropped_ids.add(str(rec.card_id))
            dropped.append(
                {
                    "dim": (card.get("scope") or {}).get("dim"),
                    "value": (card.get("scope") or {}).get("dim_value"),
                    "reason": "第二轮复审撤销：" + rec.reason,
                }
            )
        elif rec.action == ACTION_SKIP:
            # 口径问题：保留原样，只留记录。
            pass

    meta["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    kept = [card for card in cards if str(card.get("card_id")) not in dropped_ids]
    return kept, meta


def build_diagnosis(
    db_path: str | Path,
    as_of: date | str | None = None,
    window_days: int = 7,
    max_cards: int = MAX_CARDS,
    *,
    use_llm_critic: bool | None = None,
    with_material: bool = False,
    with_replan: bool = True,
) -> dict[str, Any]:
    """一次完整诊断：返回 `{as_of, window, cards, gmv, funnel, meta}`。

    `with_material=True` 时给 SKU 级卡补「主图里看到什么」——只对 R2（主图/素材）
    与 R4（尺码信息）有效，其它根因硬蹭素材没有意义。

    `with_replan=True` 时在 Critic 之后跑第二轮：同 scope 扩到 14 天找必需辅证，
    能补到的升级、补不到的撤掉（`meta.critic.replan` 里有逐卡记录）。
    """
    t0 = time.perf_counter()
    as_of_d = resolve_as_of(db_path, as_of, window_days)
    result = detect(db_path, as_of=as_of_d)

    cur = (result.window.start, result.window.end)
    # 对比窗口必须与当期等长：detect 的基线是 28 天（为了估方差），
    # 但算「少了多少钱」时拿 28 天比 7 天会把变化放大 4 倍。
    span = (cur[1] - cur[0]).days + 1
    prev_end = cur[0] - timedelta(days=1)
    prev = (prev_end - timedelta(days=span - 1), prev_end)

    bad_points = [p for p in result.points if is_bad(p)]
    groups = _group_points(bad_points)
    ordered = sorted(
        groups.values(),
        key=lambda g: -max(p.score for p in g),
    )

    cache: dict[str, ContributionResult] = {}
    cards: list[DiagnosisCard] = []
    dropped: list[dict[str, Any]] = []
    code_count: dict[str, int] = {}
    for group in ordered:
        if len(cards) >= max_cards:
            break
        card = _make_card(db_path, group, result, cur, prev, cache, len(cards) + 1)
        if card is None:
            dropped.append({"dim": group[0].dimension, "value": str(group[0].dim_value), "reason": "证据不足"})
            continue
        # 同一根因最多占 2 张卡：否则首屏会变成「5 张一模一样的 ROI 恶化」，
        # 既不好看也掩盖了别的症状。
        if code_count.get(card.code, 0) >= MAX_PER_CODE:
            dropped.append(
                {"dim": card.scope["dim"], "value": card.scope["dim_value"], "reason": f"同因已满（{card.code}）"}
            )
            continue
        code_count[card.code] = code_count.get(card.code, 0) + 1
        cards.append(card)

    cards.sort(key=lambda c: -(abs(c.gmv_impact or 0.0)))

    # Critic 放在排序之后：顺序即优先级，后面的卡会被前面的卡当作「已有更好的解释」。
    # 它只做减法（降置信度 / 驳回），不新增结论，所以不会污染 evidence 链。
    dec: Decomposition = gmv_decompose(db_path, cur, prev)
    t_critic = time.perf_counter()
    card_dicts = [c.as_dict() for c in cards]
    verdicts = critique(
        card_dicts,
        promo_skew=(result.meta.get("promo_share") or {}).get("skew"),
        market_delta_pct=dec.delta_pct,
    )
    llm_meta: dict[str, Any] = {"requested": False, "used": 0, "reason": "只用规则版"}
    settings = get_settings()
    from app.core import runtime

    want_llm = settings.critic_llm if use_llm_critic is None else use_llm_critic
    if want_llm and runtime.llm_active() == "real":
        # 模型版反方审查：只降置信度、失败静默回退，任何异常都不能拖垮诊断。
        try:
            from app.core.llm import get_client
            from app.diagnose.critic_llm import review_cards

            verdicts, llm_meta = review_cards(card_dicts, verdicts, llm=get_client())
        except Exception as exc:  # noqa: BLE001 - 审查是可选增强，坏了也要出诊断
            llm_meta = {
                "requested": True,
                "used": 0,
                "degraded": True,
                "reason": f"{type(exc).__name__}: {exc}",
            }
    kept, rejected = apply_verdicts(card_dicts, verdicts)
    for card in rejected:
        objections = (card.get("critic") or {}).get("objections") or []
        dropped.append(
            {
                "dim": card["scope"]["dim"],
                "value": card["scope"]["dim_value"],
                "reason": "Critic 驳回：" + (objections[0] if objections else "置信度不足"),
            }
        )

    # 第二轮：只救「已有 scope」的卡——扩窗看必需辅证能不能补到。
    # 不新增卡（14 天窗口会把半年前的旧异常一起捞出来），只做减法与升级。
    verdict_dicts = [v.as_dict() for v in verdicts]
    if with_replan:
        kept, replan_meta = _run_replan(db_path, kept, verdict_dicts, as_of_d, dropped)
    else:
        replan_meta = {
            "enabled": False,
            "days": REPLAN_DAYS,
            "checked": 0,
            "upgrade": 0,
            "drop": 0,
            "demote": 0,
            "skip": 0,
            "items": [],
            "elapsed_ms": 0.0,
            "degraded": False,
        }

    fn: Funnel = funnel(db_path, cur, prev)

    # 兜底：审查/复审把卡全撤了，但确实有候选——留一张「最可疑」的存疑卡，
    # 而不是返回空白。空手而归在演示里等于「系统坏了」，而一张标了证据不足的
    # 卡至少给人一个追问的入口（precision 上它本来也算 miss，不会虚高）。
    kept_weakest = False
    if not kept and card_dicts:
        best = max(card_dicts, key=lambda c: float(c.get("confidence") or 0.0))
        critic_block = dict(best.get("critic") or {})
        critic_block.update(
            {
                "verdict": "weaken",
                "confidence_before": critic_block.get("confidence_before", best.get("confidence")),
                "confidence_after": round(float(best.get("confidence") or 0.32), 4),
                "kept_as_weakest": True,
            }
        )
        best["critic"] = critic_block
        best["confidence"] = critic_block["confidence_after"]
        best.setdefault("caveats", [])
        note = "反方审查未通过，但这是本期最可疑的一处，证据不足仅供参考"
        if note not in best["caveats"]:
            best["caveats"].append(note)
        kept = [best]
        kept_weakest = True

    # 素材诊断（D9）：只给 R2/R4 的 SKU 卡补「图里看到什么」，其它根因不硬蹭。
    material_meta: dict[str, Any] = {"requested": with_material, "attached": 0}
    if with_material:
        material_meta.update(_attach_material(db_path, kept))
        for card in kept:
            block = card.get("material") or {}
            if not block:
                continue
            card.setdefault("caveats", [])
            if block.get("findings"):
                line = f"素材：{block['findings'][0]['label']}"
            else:
                # 图里挑不出毛病，本身就是一条反方意见：别把「尺码/主图」的锅甩给素材。
                line = "素材：主图各项属性正常，根因可能不在素材上"
            if line not in card["caveats"]:
                card["caveats"].append(line)

    return {
        "as_of": as_of_d.isoformat(),
        "window": _window_dict(cur[0], cur[1]),
        "baseline_window": _window_dict(prev[0], prev[1]),
        "cards": kept,
        "gmv": {
            **dec.as_dict(),
            "summary": describe_decomposition(dec),
        },
        "funnel": {
            **fn.as_dict(),
            "summary": describe_funnel(fn),
        },
        "meta": {
            "engine": "rules-v1",
            "points": len(result.points),
            "groups": len(groups),
            "cards": len(kept),
            "cards_before_critic": len(card_dicts),
            "kept_weakest": kept_weakest,
            "dropped": dropped[:10],
            "promo_share": result.meta.get("promo_share"),
            "promo_dominated": bool(result.meta.get("promo_dominated")),
            "critic": {
                "engine": "rules-v1",
                "checked": len(verdicts),
                "support": sum(1 for v in verdicts if v.verdict == "support"),
                "weaken": sum(1 for v in verdicts if v.verdict == "weaken"),
                "reject": sum(1 for v in verdicts if v.verdict == "reject"),
                "elapsed_ms": round((time.perf_counter() - t_critic) * 1000, 1),
                "llm": llm_meta,
                "replan": replan_meta,
                "verdicts": [
                    {
                        "card_id": v.card_id,
                        "code": v.code,
                        "verdict": v.verdict,
                        "confidence_before": round(v.confidence_before, 3),
                        "confidence_after": round(v.confidence_after, 3),
                        "objections": v.objections,
                    }
                    for v in verdicts
                ],
            },
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "material": material_meta,
            "note": "根因为规则候选（top-3 按置信度排序），已过 Critic 反方审查；LLM 解释见报告层。",
        },
    }
