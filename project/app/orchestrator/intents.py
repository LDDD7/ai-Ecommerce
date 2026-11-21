"""意图解析：把一句中文问题压成结构化意图。

规则版只做「够用」的事：识别想问什么（大盘 / 诊断 / 查数）、时间窗、
以及提到的款式 / 渠道 / 商品。等接了 LLM，这层变成 LLM 输出的兜底校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "diagnose": ("为什么", "原因", "诊断", "下滑", "掉了", "跌", "变差", "问题", "异常", "怎么回事", "排查"),
    "metric": ("多少", "查一下", "查查", "查询", "是多少", "占比", "对比"),
    "overview": ("怎么样", "大盘", "整体", "情况", "表现", "健康", "概括", "总结"),
}

STYLE_WORDS = ("甜美", "基础", "度假", "礼服", "通勤")
CHANNEL_WORDS = {
    "推荐": "CH_REC",
    "搜索": "CH_SEARCH",
    "直播": "CH_LIVE",
    "私域": "CH_PRIV",
    "活动": "CH_PROMO",
}
METRIC_WORDS = {
    "gmv": ("成交额", "gmv", "销售额", "营收"),
    "paid_orders": ("订单", "单量", "支付订单"),
    "cvr": ("转化率", "转化"),
    "ctr": ("点击率", "ctr"),
    "aov": ("客单价", "件单价"),
    "refund_rate": ("退款率", "退货率"),
    "roi": ("roi", "投产比", "投放"),
    "neg_review_rate": ("差评", "差评率"),
    "stockout_rate": ("断货", "缺货"),
}

_WINDOW_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"近\s*(\d{1,2})\s*天", 0),
    (r"最近\s*(\d{1,2})\s*天", 0),
    (r"过去\s*(\d{1,2})\s*天", 0),
)

_DATE_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_CN_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_SKU_RE = re.compile(r"(SKU\d{4})", re.IGNORECASE)


@dataclass(slots=True)
class Intent:
    kind: str
    question: str
    window_days: int = 7
    as_of: str | None = None
    styles: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    sku: str | None = None
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "window_days": self.window_days,
            "as_of": self.as_of,
            "styles": self.styles,
            "channels": self.channels,
            "metrics": self.metrics,
            "sku": self.sku,
            "reason": self.reason,
        }


def _kind_of(text: str) -> tuple[str, str]:
    """按关键词投票，票数相同按 diagnose > metric > overview 取更具体的。"""
    scores: dict[str, int] = {}
    hits: dict[str, str] = {}
    for kind, words in KIND_KEYWORDS.items():
        for w in words:
            if w in text:
                scores[kind] = scores.get(kind, 0) + 1
                hits.setdefault(kind, w)
    if not scores:
        return "overview", "没命中意图关键词，按大盘处理"
    order = {"diagnose": 0, "metric": 1, "overview": 2}
    best = sorted(scores, key=lambda k: (-scores[k], order.get(k, 9)))[0]
    return best, f"命中「{hits[best]}」"


def _window_of(text: str) -> int:
    for pat, _ in _WINDOW_PATTERNS:
        m = re.search(pat, text)
        if m:
            return max(1, min(30, int(m.group(1))))
    if "本周" in text or "这周" in text:
        return 7
    if "上周" in text:
        return 7
    if "本月" in text or "这个月" in text:
        return 30
    if "上月" in text or "上个月" in text:
        return 30
    return 7


def _as_of_of(text: str, today: date | None = None) -> str | None:
    m = _DATE_RE.search(text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = _CN_DATE_RE.search(text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        year = (today or date.today()).year
        try:
            return date(year, mo, d).isoformat()
        except ValueError:
            return None
    return None


def _metrics_of(text: str) -> list[str]:
    low = text.lower()
    found = []
    for name, words in METRIC_WORDS.items():
        if any(w in low for w in words):
            found.append(name)
    return found


def parse_intent(question: str, today: date | None = None) -> Intent:
    text = (question or "").strip()
    low = text.lower()
    kind, reason = _kind_of(text)
    sku_m = _SKU_RE.search(text)
    return Intent(
        kind=kind,
        question=text,
        window_days=_window_of(text),
        as_of=_as_of_of(text, today),
        styles=[s for s in STYLE_WORDS if s in text],
        channels=[v for k, v in CHANNEL_WORDS.items() if k in text],
        metrics=_metrics_of(low),
        sku=sku_m.group(1).upper() if sku_m else None,
        reason=reason,
    )
