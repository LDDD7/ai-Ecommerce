"""诊断卡的反方审查（Critic）。

为什么需要它：规则版根因只看「主症状指标」，容易把同一件事按不同名字重复讲一遍
（R2 主图衰减和 R8 流量下滑都会让点击率掉）。Critic 的职责不是再猜一次根因，
而是逐条问「这张卡凭什么成立」，把撑不住的卡降权、把自相矛盾的卡驳回。

三条原则：
1. 只做减法，不做加法：不引入新结论，只给现有结论打折。
2. 每条反驳都要带数字：没有数字的质疑等于没说。
3. 可离线：不依赖 LLM、不查库，输入就是诊断卡本身，纯函数好测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 置信度分档：>=0.55 站得住；0.30~0.55 打折但仍展示；<0.30 驳回不展示。
SUPPORT_MIN = 0.55
REJECT_BELOW = 0.30

# 最强证据的 z 低于这个值，说明「异常」可能只是波动。
FRESH_MIN_Z = 2.5
# 对成交额影响小于这个金额的卡，优先级最低（不驳回，只降权）。
MIN_IMPACT_YUAN = 500.0
# 促销日占比变化超过这个幅度，量价对比就不可比了。
PROMO_INCOMPARABLE = 0.30
# 只有「量价类」指标才对促销口径敏感；断货、退款、差评跟促销没关系。
PROMO_SENSITIVE = {"gmv", "paid_orders", "aov", "unit_price", "cvr", "roi"}
# 以下根因与促销无因果关系，即便主指标是量价类也不该被促销口径降权。
PROMO_EXEMPT = {"R3", "R7"}
# 单品跌幅不超过大盘跌幅的这个倍数时，说明它只是随大盘，归因要更谨慎。
MARKET_TOLERANCE = 1.5
# 只有这些指标与大盘 GMV 同量纲，才能直接比跌幅（客单价、曝光率换量纲后不可比）。
MARKET_METRICS = {"gmv", "paid_orders"}

# 根因 -> {指标: 期望方向}（+1 上升 / -1 下降）。方向相反直接打折。
EXPECTED_DIRECTION: dict[str, dict[str, int]] = {
    "R1": {"cvr": -1, "paid_orders": -1, "gmv": -1},
    "R2": {"ctr": -1, "clicks": -1},
    "R3": {"paid_orders": -1, "stockout_rate": +1},
    "R4": {"refund_rate": +1, "return_rate": +1},
    "R5": {"roi": -1, "cpc": +1, "ad_spend": +1},
    "R6": {"neg_review_rate": +1, "dsr": -1},
    "R7": {"impressions": -1, "paid_orders": -1},
    "R8": {"impressions": -1},
    "R9": {"cvr": -1, "unit_price": +1},
    "R10": {"refund_rate": +1},
}

# 每个根因必须有的一条「辅证」：缺了它，主指标再像也只是猜。
# R6 的辅证用 ``dsr``（均值）而不是 ``neg_review_rate``（极少条评价上的比率）——
# 后者单周只有十几条评价，一条差评就能把比率顶起来，实测是误报的主要来源。
REQUIRED_SUPPORT: dict[str, str] = {
    "R3": "stockout_rate",
    "R4": "return_rate",
    "R5": "roi",
    "R6": "dsr",
    "R9": "unit_price",
    "R10": "refund_rate",
}


@dataclass(slots=True)
class Check:
    """一条审查项。`weight` 是不过时对置信度的折扣系数。"""

    name: str
    ok: bool
    weight: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "weight": round(self.weight, 3), "detail": self.detail}


@dataclass(slots=True)
class Verdict:
    card_id: str
    code: str
    title: str
    verdict: str  # support | weaken | reject
    confidence_before: float
    confidence_after: float
    factor: float
    objections: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    source: str = "rules"

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "code": self.code,
            "title": self.title,
            "verdict": self.verdict,
            "confidence_before": round(self.confidence_before, 3),
            "confidence_after": round(self.confidence_after, 3),
            "factor": round(self.factor, 3),
            "objections": self.objections,
            "checks": [c.as_dict() for c in self.checks],
            "source": self.source,
        }


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


# 卡片本身已经写过的提示，反方不要重复（同义话题按关键词判重）。
_DEDUPE_MARKERS = ("促销日占比变化", "竞品", "同根因", "只有一条证据")
# 反方意见的前缀：写进 caveats 时用，也用于幂等判断。
_OBJECTION_PREFIX = "反方："


def _evidence_by_metric(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ev in card.get("evidence") or []:
        metric = ev.get("metric")
        if metric and metric not in out:
            out[metric] = ev
    return out


def _check_direction(card: dict[str, Any], ev: dict[str, dict[str, Any]]) -> Check:
    """主指标的方向是否与根因预期一致；其它预期指标自相矛盾时也要点出来。"""
    code = card.get("code") or ""
    metric = card.get("primary_metric") or ""
    expect = (EXPECTED_DIRECTION.get(code) or {}).get(metric)
    item = ev.get(metric) or {}
    delta = item.get("delta")
    if expect is None or delta is None:
        return Check("direction", True, 1.0, "方向无需校验")
    ok = (delta > 0 and expect > 0) or (delta < 0 and expect < 0)
    if not ok:
        word = "上升" if expect > 0 else "下降"
        return Check(
            "direction",
            False,
            0.45,
            f"主指标 {metric} {_pct(item.get('delta_pct'))}，根因 {code} 本应{word}",
        )
    # 主指标对了，但同一根因的其它证据反向走：不扣分，只写一条反方（自相矛盾需要人看）。
    clash = []
    for name, want in (EXPECTED_DIRECTION.get(code) or {}).items():
        if name == metric:
            continue
        other = ev.get(name) or {}
        d = other.get("delta")
        if d is None:
            continue
        if (d > 0 and want < 0) or (d < 0 and want > 0):
            clash.append(f"{name} {_pct(other.get('delta_pct'))}")
    if clash:
        return Check(
            "direction",
            False,
            1.0,
            f"根因 {code} 的主指标成立，但辅证方向相反：{'、'.join(clash[:2])}",
        )
    return Check("direction", True, 1.0, f"{metric} 方向符合预期")


def _check_upstream(card: dict[str, Any], ev: dict[str, dict[str, Any]], promo_skew: float | None) -> Check:
    """更上游的解释是否更可能：曝光 vs 点击率、促销 vs 自然下滑、辅证是否在。"""
    code = card.get("code") or ""
    imp = ev.get("impressions") or {}
    ctr = ev.get("ctr") or {}
    d_imp = imp.get("delta_pct")
    d_ctr = ctr.get("delta_pct")

    if code == "R2" and d_imp is not None and d_ctr is not None:
        if d_imp <= -0.10 and abs(d_imp) >= abs(d_ctr):
            return Check(
                "upstream",
                False,
                0.70,
                f"曝光也在下滑（{_pct(d_imp)}），跌幅不亚于点击率（{_pct(d_ctr)}），更像流量结构变化而非主图衰减",
            )
    if code == "R8" and d_ctr is not None:
        if d_imp is None:
            if d_ctr <= -0.10:
                return Check(
                    "upstream",
                    False,
                    0.75,
                    f"缺少曝光证据，无法排除流量结构变化（点击率 {_pct(d_ctr)}）",
                )
        elif d_ctr <= -0.10 and abs(d_imp) < abs(d_ctr):
            return Check(
                "upstream",
                False,
                0.70,
                f"点击率跌幅（{_pct(d_ctr)}）大于曝光跌幅（{_pct(d_imp)}），更像素材/相关性下降而非竞争加剧",
            )
    if code == "R7":
        if promo_skew is None:
            return Check(
                "upstream",
                False,
                0.85,
                "缺少促销日占比数据，无法证实大促透支",
            )
        if abs(promo_skew) < 0.15:
            return Check(
                "upstream",
                False,
                0.70,
                f"促销日占比变化只有 {promo_skew:+.2f}，缺少大促透支的直接证据",
            )

    required = REQUIRED_SUPPORT.get(code)
    if required:
        item = ev.get(required) or {}
        delta = item.get("delta")
        if delta is None:
            return Check("upstream", False, 0.80, f"缺少辅证指标 {required}，根因 {code} 只有主症状支撑")
        expect = (EXPECTED_DIRECTION.get(code) or {}).get(required)
        if expect is not None:
            wrong = (delta > 0 and expect < 0) or (delta < 0 and expect > 0)
            if wrong:
                return Check(
                    "upstream",
                    False,
                    0.75,
                    f"辅证 {required} {_pct(item.get('delta_pct'))}，与根因 {code} 的预期方向相反",
                )

    if code == "R1":
        gap = card.get("competitor_gap")
        if gap is None:
            return Check("upstream", False, 0.85, "缺少竞品价格对比，竞品降价战无法证实")
        if gap < 0.03:
            return Check("upstream", False, 0.75, f"我方价格仅高于竞品 {gap * 100:.1f}%，不足以解释转化下滑")

    return Check("upstream", True, 1.0, "更上游的解释不成立")


def _check_sample(card: dict[str, Any]) -> Check:
    """证据强度：最强一条的 z 够不够，证据条数够不够。"""
    zs = [abs(ev.get("z") or 0.0) for ev in card.get("evidence") or []]
    strongest = max(zs) if zs else 0.0
    if strongest < FRESH_MIN_Z:
        return Check(
            "sample",
            False,
            0.80,
            f"最强证据 z={strongest:.1f}，低于 {FRESH_MIN_Z}，可能只是正常波动",
        )
    if len(card.get("evidence") or []) <= 1:
        return Check("sample", False, 0.90, "只有一条证据，结论偏薄")
    return Check("sample", True, 1.0, f"最强证据 z={strongest:.1f}")


def _check_promo(card: dict[str, Any], promo_skew: float | None) -> Check:
    """促销日占比变化太大时，量价不可比。

    R7 本身就是促销；R3 断货与促销无因果，都不受此限。
    """
    if card.get("code") in PROMO_EXEMPT or promo_skew is None:
        return Check("promo", True, 1.0, "促销口径不冲突")
    if (card.get("primary_metric") or "") not in PROMO_SENSITIVE:
        return Check("promo", True, 1.0, "该指标不受促销口径影响")
    if abs(promo_skew) >= PROMO_INCOMPARABLE:
        return Check(
            "promo",
            False,
            0.65,
            f"窗口促销日占比变化 {promo_skew:+.2f}，量价对比不可比",
        )
    return Check("promo", True, 1.0, "促销口径不冲突")


def _check_market(card: dict[str, Any], market_delta_pct: float | None) -> Check:
    """这张卡是不是只是随大盘：单店/全店级别不适用，只查单品与款式。"""
    dim = (card.get("scope") or {}).get("dim")
    if dim in (None, "", "all") or market_delta_pct is None:
        return Check("market", True, 1.0, "全店口径无需与大盘对比")
    # 只有成交额类指标才和大盘 GMV 可比；曝光、退款率跟大盘 GMV 不是一个量纲。
    if (card.get("primary_metric") or "") not in MARKET_METRICS:
        return Check("market", True, 1.0, "该指标与大盘不同量纲")

    ev = _evidence_by_metric(card)
    primary = ev.get(card.get("primary_metric") or "") or {}
    delta_pct = primary.get("delta_pct")
    if delta_pct is None:
        return Check("market", True, 1.0, "缺少跌幅数据")
    # 只有同向（都跌）时才有「随大盘」的问题；逆势上涨的卡不适用。
    if delta_pct > 0 and market_delta_pct > 0 and abs(delta_pct) <= abs(market_delta_pct) * MARKET_TOLERANCE:
        return Check(
            "market",
            False,
            0.85,
            f"该卡 {delta_pct * 100:+.1f}% 与大盘 {market_delta_pct * 100:+.1f}% 同量级，可能只是大盘带动",
        )
    if delta_pct < 0 and market_delta_pct < 0 and abs(delta_pct) <= abs(market_delta_pct) * MARKET_TOLERANCE:
        return Check(
            "market",
            False,
            0.80,
            f"该卡 {delta_pct * 100:+.1f}% 与大盘 {market_delta_pct * 100:+.1f}% 同量级，难以单独归因",
        )
    return Check("market", True, 1.0, "跌幅明显大于大盘")


def _check_impact(card: dict[str, Any]) -> Check:
    """这张卡值不值得排在前面：影响金额是否可信、是否够大。"""
    impact = card.get("gmv_impact")
    share = card.get("impact_share")
    if impact is None:
        return Check("impact", False, 0.90, "算不出成交额影响，无法排优先级")
    if abs(impact) < MIN_IMPACT_YUAN:
        return Check("impact", False, 0.80, f"对成交额影响只有 {impact:+,.0f} 元，优先级低")
    if share is not None and abs(share) > 1.0:
        return Check("impact", False, 0.85, f"占整体变化 {share * 100:.0f}%，超过 100% 口径可疑")
    return Check("impact", True, 1.0, f"影响成交额 {impact:+,.0f} 元")


def _check_duplicate(card: dict[str, Any], seen: set[str]) -> Check:
    """同根因重复出现：第二张开始降权（调用方按影响金额降序传卡）。"""
    code = card.get("code") or ""
    if code in seen:
        return Check("duplicate", False, 0.85, f"同根因 {code} 已有一张卡，本张优先级靠后")
    return Check("duplicate", True, 1.0, "根因不重复")


def critique(
    cards: list[dict[str, Any]],
    *,
    promo_skew: float | None = None,
    market_delta_pct: float | None = None,
) -> list[Verdict]:
    """逐张审查。顺序即优先级：前面的卡会被后面的卡当作「已有更好的解释」。"""
    verdicts: list[Verdict] = []
    seen: set[str] = set()
    for card in cards:
        ev = _evidence_by_metric(card)
        checks = [
            _check_direction(card, ev),
            _check_upstream(card, ev, promo_skew),
            _check_market(card, market_delta_pct),
            _check_sample(card),
            _check_promo(card, promo_skew),
            _check_impact(card),
            _check_duplicate(card, seen),
        ]
        factor = 1.0
        objections: list[str] = []
        for check in checks:
            if check.ok:
                continue
            factor *= check.weight
            objections.append(check.detail)
        before = float(card.get("confidence") or 0.0)
        after = max(0.05, min(0.98, before * factor))
        if after < REJECT_BELOW:
            verdict = "reject"
        elif after < SUPPORT_MIN:
            verdict = "weaken"
        else:
            verdict = "support"
        verdicts.append(
            Verdict(
                card_id=str(card.get("card_id") or ""),
                code=str(card.get("code") or ""),
                title=str(card.get("title") or ""),
                verdict=verdict,
                confidence_before=before,
                confidence_after=after,
                factor=factor,
                objections=objections,
                checks=checks,
            )
        )
        # 被驳回的卡不占「同根因」名额：它已经不在报告里了，不该拖累下一张。
        if verdict != "reject":
            seen.add(str(card.get("code") or ""))
    return verdicts


def describe(v: Verdict) -> str:
    """一句话中文结论，给报告与日志用。"""
    word = {"support": "成立", "weaken": "存疑", "reject": "驳回"}[v.verdict]
    head = f"{v.code} {v.title}：{word}（置信度 {v.confidence_before:.2f}→{v.confidence_after:.2f}）"
    if not v.objections:
        return head + "。"
    return head + "；反方：" + "；".join(v.objections[:2]) + "。"


def apply_verdicts(
    cards: list[dict[str, Any]],
    verdicts: list[Verdict],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把审查结果写回卡片：改置信度、追反方意见、把驳回的卡摘出去。

    返回 `(保留的卡, 被驳回的卡)`。被驳回的卡不删字段，只是从展示列表里移走，
    这样 meta.dropped 里能说清楚「为什么这张卡不见了」。
    """
    by_id = {v.card_id: v for v in verdicts}
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for card in cards:
        v = by_id.get(str(card.get("card_id") or ""))
        if v is None:
            kept.append(card)
            continue
        card["confidence"] = round(v.confidence_after, 3)
        card["critic"] = {
            "verdict": v.verdict,
            "confidence_before": round(v.confidence_before, 3),
            "factor": round(v.factor, 3),
            "objections": v.objections,
        }
        if v.objections:
            caveats = list(card.get("caveats") or [])
            joined = " ".join(caveats)
            for o in v.objections[:2]:
                line = f"{_OBJECTION_PREFIX}{o}"
                if line in caveats:  # 幂等：重复调用不重复追加
                    continue
                # 同一件事不要在两个栏目里各说一遍（例如促销偏斜，卡片自带提示 + 反方质疑）。
                if any(marker in o and marker in joined for marker in _DEDUPE_MARKERS):
                    continue
                caveats.append(line)
            card["caveats"] = caveats
        if v.verdict == "reject":
            rejected.append(card)
        else:
            kept.append(card)
    return kept, rejected
