"""根因假设：证据模式 → 候选根因（规则版）。

分工和 spec §7.6 一致：**规则负责给候选，LLM 只负责选下一步下钻和写解释**。
这样即使没有 API key（`LLM_MODE=mock`），诊断卡也是有依据的；有 key 时只是
把候选排序与措辞做得更好，不会凭空发明根因。

根因码与合成数据里注入的 10 类异常一一对应（见 spec §5.3），所以评测可以
直接拿 `anomaly_gt` 当标准答案。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

CODE_TITLES: dict[str, str] = {
    "R1": "竞品降价战",
    "R2": "主图素材衰减",
    "R3": "热销断货",
    "R4": "尺码与描述不符",
    "R5": "投放 ROI 恶化",
    "R6": "差评爆发",
    "R7": "大促后透支",
    "R8": "竞争加剧 / 流量下滑",
    "R9": "价格带错配",
    "R10": "物流时效恶化",
}

# 指标 → 候选根因（按先验强弱排序，第二个数字是基础置信度）
METRIC_RULES: dict[str, list[tuple[str, float]]] = {
    "ctr": [("R2", 0.85), ("R8", 0.45)],
    "clicks": [("R2", 0.60), ("R8", 0.50)],
    "impressions": [("R8", 0.70), ("R7", 0.50)],
    "detail_uv": [("R8", 0.60), ("R7", 0.40)],
    "paid_orders": [("R3", 0.60), ("R7", 0.50), ("R1", 0.40)],
    "gmv": [("R3", 0.50), ("R9", 0.40)],
    "cvr": [("R1", 0.75), ("R4", 0.50), ("R6", 0.50), ("R9", 0.40)],
    "aov": [("R9", 0.70)],
    "unit_price": [("R9", 0.70)],
    "refund_rate": [("R4", 0.70), ("R10", 0.50)],
    "return_rate": [("R4", 0.90)],
    # 差评率是「少数几条评价」上的比率，单周波动大；评分（dsr）是均值，更稳。
    # 所以 R6 的强证据给 dsr，neg_review_rate 作为辅证。
    "neg_review_rate": [("R6", 0.70)],
    "dsr": [("R6", 0.80)],
    "roi": [("R5", 0.85)],
    "ad_spend": [("R5", 0.40)],
    "cpc": [("R5", 0.50)],
    "stockout_rate": [("R3", 0.90)],
}

PROMO_CODES = {"R7"}
PROMO_PENALTY = 0.20


@dataclass
class Candidate:
    code: str
    title: str
    base_conf: float
    why: str = ""
    confidence: float = 0.0
    promoted: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.promoted is None:
            self.promoted = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "base_conf": round(self.base_conf, 3),
            "confidence": round(self.confidence, 3),
            "why": self.why,
            "promoted": self.promoted,
        }


_METRIC_WHY = {
    "ctr": "曝光没掉、点击率掉了，问题在素材/标题的吸引力",
    "impressions": "曝光量本身下滑，先看流量入口与竞品动作",
    "cvr": "详情页访客没少、成交变少，多半是价格或转化链路",
    "paid_orders": "订单数下滑，需区分是流量问题还是转化问题",
    "refund_rate": "退款率抬升，看退款原因分布（尺码/物流/质量）",
    "return_rate": "尺码退货率抬升，指向尺码表与版型描述",
    "neg_review_rate": "差评率抬升，指向口碑突发事件",
    "dsr": "评分下滑，指向履约与商品一致性",
    "roi": "投放 ROI 恶化，看花费与产出是否脱钩",
    "stockout_rate": "断货率抬升，热销款可能已经售罄",
    "aov": "客单价变化，先确认价格带与搭配销售",
    "gmv": "成交额下滑，需拆成流量/转化/客单价三因子",
}


def candidates_for(metric: str) -> list[Candidate]:
    """按指标给出候选根因（未打分）。"""
    rules = METRIC_RULES.get(metric, [])
    why = _METRIC_WHY.get(metric, "")
    return [Candidate(code=code, title=CODE_TITLES[code], base_conf=base, why=why) for code, base in rules]


def competitor_gap(
    db_path: str | Path,
    sku_id: str,
    window: tuple[date, date],
) -> dict[str, Any] | None:
    """我方售价 vs 竞品最低价：返回 {our, competitor_min, gap}。

    `fact_competitor_price` 是生数据，没有进指标语义层（竞品价不是经营指标），
    这里只做只读查询，供 R1 的证据引用。
    """
    p = Path(db_path)
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT price_tag FROM dim_sku WHERE sku_id = ?", (sku_id,)).fetchone()
        if not row or not row[0]:
            return None
        our = float(row[0])
        comp = con.execute(
            "SELECT MIN(price) FROM fact_competitor_price WHERE sku_id = ? AND dt BETWEEN ? AND ?",
            (sku_id, window[0].isoformat(), window[1].isoformat()),
        ).fetchone()
    finally:
        con.close()
    if not comp or comp[0] is None:
        return None
    comp_min = float(comp[0])
    if comp_min <= 0:
        return None
    return {"our": our, "competitor_min": comp_min, "gap": (our - comp_min) / comp_min}


def refund_split(
    db_path: str | Path,
    dimension: str | None,
    dim_value: str | None,
    cur: tuple[date, date],
    base: tuple[date, date],
) -> dict[str, Any] | None:
    """退款结构：总退款率 vs 尺码类退货率，用来区分 R4（尺码）与 R10（物流）。

    - 总退款率明显上升、尺码类基本不动 → 更像是物流/售后时效（R10）
    - 尺码类同步明显上升 → 更像是描述与实物不符（R4）
    """
    from app.insights.overview import _query

    try:
        cols_c, rows_c = _query(
            db_path,
            ["refund_rate", "return_rate"],
            date.fromisoformat(str(cur[0])),
            date.fromisoformat(str(cur[1])),
            dims=[dimension] if dimension else None,
        )
        cols_b, rows_b = _query(
            db_path,
            ["refund_rate", "return_rate"],
            date.fromisoformat(str(base[0])),
            date.fromisoformat(str(base[1])),
            dims=[dimension] if dimension else None,
        )
    except Exception:  # 结构分析拿不到就退化成中性，不能影响出卡
        return None

    def _pick(cols: list[str], rows: list[list[Any]]) -> dict[str, float] | None:
        for row in rows:
            if dimension and str(row[cols.index(dimension)]) != str(dim_value):
                continue
            return {
                "refund_rate": float(row[cols.index("refund_rate")] or 0.0),
                "return_rate": float(row[cols.index("return_rate")] or 0.0),
            }
        return None

    picked_c, picked_b = _pick(cols_c, rows_c), _pick(cols_b, rows_b)
    if not picked_c or not picked_b:
        return None

    def _rel(now: float, before: float) -> float:
        return (now - before) / before if before > 0 else 0.0

    refund_rel = _rel(picked_c["refund_rate"], picked_b["refund_rate"])
    return_rel = _rel(picked_c["return_rate"], picked_b["return_rate"])
    verdict = None
    if refund_rel >= 0.15 and return_rel < 0.10:
        verdict = "R10"
    elif return_rel >= 0.20:
        verdict = "R4"
    return {
        "refund_rel": round(refund_rel, 4),
        "return_rel": round(return_rel, 4),
        "verdict": verdict,
    }


def score(
    cand: Candidate,
    *,
    z: float | None,
    support: int,
    promo_skew: float | None = None,
    gap: float | None = None,
    split: dict[str, Any] | None = None,
) -> Candidate:
    """把先验、显著性、支撑证据数、促销偏斜揉成一个 0~0.95 的置信度。"""
    conf = cand.base_conf
    if z is not None:
        conf += min(0.15, max(0.0, (abs(z) - 2.0) * 0.02))
    if support > 1:
        conf += min(0.10, 0.04 * (support - 1))
    if cand.code in PROMO_CODES and promo_skew is not None and abs(promo_skew) > 0.15:
        conf += 0.10
        cand.promoted.append("promo_window")
    if cand.code == "R1" and gap is not None and gap >= 0.05:
        conf += 0.12
        cand.promoted.append("competitor_price_gap")
    # 退款结构：总退款率涨而尺码类不动 → 物流；尺码类同步涨 → 尺码
    if split and split.get("verdict"):
        verdict = split["verdict"]
        if cand.code == verdict:
            conf += 0.15 if verdict == "R10" else 0.10
            cand.promoted.append("refund_structure")
        elif cand.code in {"R4", "R10"}:
            conf -= 0.15
            cand.promoted.append("refund_structure_against")
    if promo_skew is not None and abs(promo_skew) > 0.15 and cand.code not in PROMO_CODES:
        conf -= PROMO_PENALTY
        cand.promoted.append("promo_window_penalty")
    cand.confidence = max(0.2, min(0.95, conf))
    return cand


def rank(
    metric: str,
    *,
    z: float | None = None,
    support: int = 1,
    promo_skew: float | None = None,
    gap: float | None = None,
    split: dict[str, Any] | None = None,
    limit: int = 3,
) -> list[Candidate]:
    """给一个指标算出 top-N 候选根因，按置信度降序。"""
    scored = [
        score(c, z=z, support=support, promo_skew=promo_skew, gap=gap, split=split)
        for c in candidates_for(metric)
    ]
    scored.sort(key=lambda c: -c.confidence)
    return scored[:limit]


def explain(cand: Candidate) -> str:
    why = cand.why or f"命中规则：{cand.title}"
    return f"{cand.code} {cand.title}（置信度 {cand.confidence:.0%}）：{why}"


def codes_in(items: Iterable[Candidate]) -> list[str]:
    return [c.code for c in items]
