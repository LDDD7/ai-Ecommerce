"""合成数据生成器：可复现、注入已知根因、带 ground truth。

设计要点
- 只用 stdlib + Pillow（不引 Faker/pandas），固定随机种子，重跑结果一致。
- 先按「SKU 热度 × 渠道权重 × 日效应」造曝光，再按漏斗率下推到订单；注入异常通过
  乘法因子作用在对应环节（ctr / cvr / refund / spend / impressions），因此「指标变了、
  但根因是哪一个」有 ground truth 可对。
- 素材图用 Pillow 程序化渲染，属性即标签，同时写 image_feature(source='mock')。

用法
    python -m app.data.seed --sku-count 300 --days 180 --orders 200000
    python -m app.data.seed --tiny          # 20 SKU / 30 天，快速自检
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
STORE_ID = "store_001"

# ---------------------------------------------------------------- 词表

CHANNELS = [
    ("CH_REC", "推荐", "推荐"),
    ("CH_SEARCH", "搜索", "搜索"),
    ("CH_LIVE", "直播", "直播"),
    ("CH_PRIV", "私域", "私域"),
    ("CH_PROMO", "活动", "活动"),
]
CHANNEL_W = {"CH_REC": 1.00, "CH_SEARCH": 0.78, "CH_LIVE": 0.32, "CH_PRIV": 0.26, "CH_PROMO": 0.44}

SEGMENTS = [
    ("SEG_STU", "18-24 学生", "18-24", "高", "二线"),
    ("SEG_OFFICE", "25-30 白领", "25-30", "中", "一线"),
    ("SEG_COMMUTE", "31-35 通勤", "31-35", "中", "一线"),
    ("SEG_FAMILY", "36-45 家庭", "36-45", "低", "三线"),
    ("SEG_HUNTER", "价格猎手", "25-35", "高", "二线"),
]

STYLES = [
    ("通勤", ["修身", "直筒", "衬衫式"]),
    ("度假", ["法式", "碎花", "吊带"]),
    ("甜美", ["泡泡袖", "娃娃领", "收腰"]),
    ("基础", ["纯色", "针织", "长袖"]),
    ("礼服", ["缎面", "鱼尾", "高腰"]),
]
FABRICS = ["雪纺", "棉", "真丝", "针织", "醋酸", "混纺"]
COLORS = ["米白", "雾霾蓝", "黑色", "豆沙粉", "橄榄绿", "藏青", "燕麦色"]
SUPPLIERS = ["SUP_HZ01", "SUP_HZ02", "SUP_GZ01", "SUP_SZ01", "SUP_SQ01"]
COMPETITORS = ["竞品A", "竞品B", "竞品C"]
REFUND_REASONS = [
    ("SIZE_SMALL", "尺码偏小", 0.42),
    ("COLOR_DIFF", "色差", 0.19),
    ("QUALITY", "质量", 0.16),
    ("LOGISTICS", "物流慢", 0.13),
    ("DISLIKE", "不喜欢", 0.10),
]

# 根因决定「退款原因」的结构：尺码问题 → SIZE_SMALL 飙升，物流变慢 → LOGISTICS 飙升。
# 没有这条，R4（尺码与描述不符）与 R10（物流时效恶化）在数据上完全不可分——两者都只是
# 「退款率上升」，而归因只能靠原因码结构，这也是真实电商里区分它们的方式。
REASON_BIAS = {"R4": "SIZE_SMALL", "R10": "LOGISTICS"}
REASON_BIAS_RATE = 0.80  # 窗口内 80% 的退款走该原因，留 20% 噪声

PROMOS = [
    ("618 大促", date(2026, 6, 1), date(2026, 6, 18)),
    ("99 划算节", date(2026, 9, 1), date(2026, 9, 9)),
    ("双 11", date(2026, 10, 20), date(2026, 11, 11)),
]

REVIEW_TAGS = ["尺码", "色差", "质量", "物流", "好评"]


# ---------------------------------------------------------------- 数据结构


@dataclass
class Sku:
    sku_id: str
    title: str
    style: str
    fabric: str
    color: str
    price_tag: float
    cost: float
    launch: date
    image_version: int
    supplier: str
    price_band: str
    weight: float = 1.0
    channels: list[str] = field(default_factory=list)
    base_ctr: float = 0.05
    base_cart: float = 0.09
    base_pay: float = 0.28
    base_refund: float = 0.10
    is_ad: bool = False
    # 素材属性（渲染与 ground truth）
    text_coverage: float = 0.0
    model_present: bool = True
    scene: str = "室内棚拍"
    background: str = "纯色"
    size_chart: bool = True
    watermark: bool = False
    collage: bool = False
    text_blocks: list[str] = field(default_factory=list)


@dataclass
class Anomaly:
    code: str
    name: str
    start: date
    end: date
    skus: set[str] | None  # None = 全店
    effects: dict[str, float]
    magnitude: str
    root_cause: str
    note: str = ""

    def matches(self, sku_id: str, dt: date) -> bool:
        if not (self.start <= dt <= self.end):
            return False
        return self.skus is None or sku_id in self.skus


# ---------------------------------------------------------------- 工具函数


def daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def promo_of(d: date) -> tuple[int, str | None]:
    for name, s, e in PROMOS:
        if s <= d <= e:
            return 1, name
    return 0, None


def season_of(d: date) -> str:
    return {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春",
            6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}[d.month]


def dow_factor(dow: int) -> float:
    # 周一=0
    return [0.92, 0.90, 0.95, 1.00, 1.06, 1.16, 1.10][dow]


def month_factor(d: date) -> float:
    # 连衣裙：夏季高峰，冬季低谷
    return {5: 0.95, 6: 1.25, 7: 1.35, 8: 1.15, 9: 1.00, 10: 0.95,
            11: 1.30, 12: 0.80, 1: 0.70, 2: 0.72, 3: 0.85, 4: 0.92}[d.month]


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def jitter(rng: random.Random, sigma: float = 0.08) -> float:
    return clamp(rng.gauss(1.0, sigma), 0.72, 1.38)


# ---------------------------------------------------------------- 造维度


def build_days(days: int, end: date) -> list[date]:
    return [end - timedelta(days=i) for i in range(days - 1, -1, -1)]


def build_skus(rng: random.Random, n: int, days: list[date]) -> list[Sku]:
    start = days[0]
    span = (days[-1] - start).days
    skus: list[Sku] = []
    for i in range(n):
        style, subs = rng.choice(STYLES)
        fabric = rng.choice(FABRICS)
        color = rng.choice(COLORS)
        price = round(rng.choice([69, 89, 99, 129, 149, 169, 199, 239, 269, 329, 399, 499])
                      * rng.uniform(0.95, 1.12), 0)
        cost = round(price * rng.uniform(0.30, 0.46), 2)
        band = "低价" if price < 120 else ("中价" if price < 260 else "高价")
        launch = start + timedelta(days=rng.randint(0, max(1, span - 30)))
        sku = Sku(
            sku_id=f"SKU{i + 1:04d}",
            title=f"{color}{fabric}{rng.choice(subs)}{rng.choice(['连衣裙', '长裙', 'A 字裙'])}",
            style=style,
            fabric=fabric,
            color=color,
            price_tag=float(price),
            cost=cost,
            launch=launch,
            image_version=rng.randint(1, 3),
            supplier=rng.choice(SUPPLIERS),
            price_band=band,
        )
        # 热度：帕累托分布，头部少数 SKU 吃掉大部分 GMV
        sku.weight = rng.paretovariate(1.35) * rng.uniform(0.6, 1.4)
        # 渠道分配：每个 SKU 2~3 个主渠道
        chans = rng.sample(["CH_REC", "CH_SEARCH", "CH_LIVE", "CH_PRIV", "CH_PROMO"], k=rng.choice([2, 2, 3]))
        sku.channels = chans
        sku.base_ctr = rng.uniform(0.028, 0.075)
        sku.base_cart = rng.uniform(0.05, 0.14)
        sku.base_pay = rng.uniform(0.18, 0.38)
        sku.base_refund = rng.uniform(0.06, 0.16)
        sku.is_ad = rng.random() < 0.32
        # 素材属性：约 22% 缺尺码表，约 18% 文字占比过高
        sku.size_chart = rng.random() > 0.22
        sku.text_coverage = rng.choice([0.0, 0.08, 0.14, 0.22, 0.38, 0.44])
        sku.model_present = rng.random() > 0.25
        sku.scene = rng.choice(["室内棚拍", "街拍", "度假场景", "纯白底"])
        sku.background = "纯白" if sku.scene == "纯白底" else rng.choice(["纯色", "渐变", "场景"])
        sku.watermark = rng.random() < 0.12
        sku.collage = rng.random() < 0.10
        sku.text_blocks = make_text_blocks(rng, sku)
        skus.append(sku)
    return skus


def make_text_blocks(rng: random.Random, sku: Sku) -> list[str]:
    if sku.text_coverage <= 0:
        return []
    pool = [f"{sku.color}{sku.fabric}", f"{sku.style}显瘦", "3 色可选", "送礼首选", "限时 ¥{:.0f}".format(sku.price_tag)]
    k = 1 if sku.text_coverage <= 0.14 else 2
    return rng.sample(pool, k)


# ---------------------------------------------------------------- 造异常


def build_anomalies(rng: random.Random, skus: list[Sku], days: list[date]) -> list[Anomaly]:
    end = days[-1]

    def pick(n: int, pred=None) -> set[str]:
        pool = [s for s in skus if (pred is None or pred(s))]
        return {s.sku_id for s in rng.sample(pool, min(n, len(pool)))}

    commute = pick(40, lambda s: s.style == "通勤")
    stale = pick(24)
    stockout = pick(12)
    size_issue = pick(18, lambda s: not s.size_chart)
    ad_bad = pick(26, lambda s: s.is_ad)
    neg_burst = pick(10)
    compet = pick(20)
    price_up = pick(9, lambda s: s.price_band == "高价")
    logistics = pick(22)

    return [
        Anomaly("R1", "竞品降价战", date(2026, 10, 25), date(2026, 11, 6), commute,
                {"cvr": 0.80, "ctr": 0.95, "competitor_price": 0.88},
                "竞品价 -12%，我方 cvr -20%", "竞品降价",
                "竞品 A/B 在同款上做满减，直接压价 12%"),
        Anomaly("R2", "主图素材衰减", date(2026, 9, 15), date(2026, 10, 5), stale,
                {"ctr": 0.62, "cvr": 0.92}, "ctr -38%，无款式/素材更新",
                "素材老化", "同批主图用了 90 天未换版，点击率持续下滑"),
        Anomaly("R3", "热销断货", date(2026, 11, 8), date(2026, 11, 14), stockout,
                {"orders": 0.38, "stockout": 1.0}, "订单 -62%，库存 0",
                "库存断货", "双 11 后备货不足，爆款连续 7 天无货"),
        Anomaly("R4", "尺码信息缺失", date(2026, 6, 10), date(2026, 7, 10), size_issue,
                {"refund": 1.9, "cvr": 0.94, "neg_review": 1.8},
                "退款率 +9pp，主图无尺码表", "尺码信息缺失",
                "主图与详情页都没有尺码表，退货原因集中在「尺码偏小」"),
        Anomaly("R5", "投放 ROI 恶化", date(2026, 9, 20), date(2026, 10, 10), ad_bad,
                {"spend": 1.45, "cvr": 0.88}, "花费 +45%，ROI -32%",
                "投放效率下降", "出价被抬，CPC 上涨但转化没跟上"),
        Anomaly("R6", "差评爆发", date(2026, 8, 5), date(2026, 8, 20), neg_burst,
                {"cvr": 0.85, "neg_review": 2.6, "rating": -0.9},
                "差评率 +15pp，cvr -15%", "口碑下滑",
                "一批货出现色差，评价区出现集中差评"),
        Anomaly("R7", "大促后疲劳", date(2026, 11, 12), date(2026, 11, 18), None,
                {"impressions": 0.60, "cvr": 0.90}, "流量 -40%，cvr -10%",
                "促销透支", "双 11 后自然流量回落，加购率同步下降"),
        Anomaly("R8", "竞争加剧", date(2026, 7, 1), date(2026, 7, 25), compet,
                {"impressions": 0.72, "ctr": 0.92}, "曝光 -28%，ctr -8%",
                "竞品抢流量", "同类目新品牌密集投放，搜索词被分流"),
        Anomaly("R9", "价格带错配", date(2026, 10, 1), date(2026, 10, 20), price_up,
                {"cvr": 0.78, "price": 1.12}, "cvr -22%，售价 +12%",
                "定价过高", "对标竞品后本价位段缺乏竞争力"),
        Anomaly("R10", "物流时效恶化", date(2026, 10, 10), date(2026, 10, 25), logistics,
                {"refund": 1.5, "cvr": 0.95, "neg_review": 1.4},
                "退款率 +5pp，物流慢占比升", "履约时效",
                "合作仓爆仓，华东地区签收延迟 2-3 天"),
    ]


def rescale_anomalies(anomalies: list[Anomaly], days: list[date]) -> list[Anomaly]:
    """把「180 天基准」的注入窗口按比例映射到实际区间。

    `--tiny` 只有 30 天，若不映射，10 类根因大多落在窗口外，自检就测不到。
    """
    span = (days[-1] - days[0]).days + 1
    if span >= 150:
        return anomalies
    ref = date(2026, 11, 18)
    k = span / 180.0
    out: list[Anomaly] = []
    for a in anomalies:
        s_off = max(1, round((ref - a.start).days * k))
        e_off = max(0, round((ref - a.end).days * k))
        if e_off > s_off:
            s_off, e_off = e_off, s_off
        s = max(days[0], days[-1] - timedelta(days=s_off))
        e = max(s, days[-1] - timedelta(days=e_off))
        out.append(Anomaly(a.code, a.name, s, e, a.skus, a.effects,
                           a.magnitude, a.root_cause, a.note))
    return out


def effects_index(anomalies: list[Anomaly]) -> dict[tuple[str, date], list[dict[str, float]]]:
    idx: dict[tuple[str, date], list[dict[str, float]]] = {}
    for a in anomalies:
        for d in daterange(a.start, a.end):
            if a.skus is None:
                # 全店：用一个特殊键在迭代 SKU 时展开，见 apply_effects
                idx.setdefault(("*", d), []).append(a.effects)
            else:
                for sid in a.skus:
                    idx.setdefault((sid, d), []).append(a.effects)
    return idx


def apply_effects(idx, sku_id: str, d: date) -> dict[str, float]:
    merged: dict[str, float] = {}
    for eff in idx.get((sku_id, d), []) + idx.get(("*", d), []):
        for k, v in eff.items():
            merged[k] = merged.get(k, 1.0) * v
    return merged


def reason_bias_index(anomalies: list[Anomaly]) -> dict[tuple[str, date], str]:
    """(sku, 下单日) → 应当占主导的退款原因码。

    只给「有退款结构含义」的两个根因建索引：R4 尺码、R10 物流。
    全店级注入（skus is None）没有具体 SKU，跳过——用默认分布即可。
    """
    idx: dict[tuple[str, date], str] = {}
    for a in anomalies:
        reason = REASON_BIAS.get(a.code)
        if not reason or a.skus is None:
            continue
        for d in daterange(a.start, a.end):
            for sid in a.skus:
                idx[(sid, d)] = reason
    return idx


# ---------------------------------------------------------------- 造事实


def gen_facts(
    rng: random.Random,
    skus: list[Sku],
    days: list[date],
    anomalies: list[Anomaly],
    orders_target: int,
    con: sqlite3.Connection,
) -> dict[str, int]:
    idx = effects_index(anomalies)
    reason_bias = reason_bias_index(anomalies)

    # 第一遍：算相对权重，标定 K 使订单量接近目标
    cells: list[tuple[str, date, str, float]] = []  # sku, dt, channel, weight
    for d in days:
        mf = month_factor(d) * dow_factor(d.weekday()) * (1.55 if promo_of(d)[0] else 1.0)
        for s in skus:
            for ch in s.channels:
                w = s.weight * CHANNEL_W[ch] * mf * jitter(rng, 0.10)
                cells.append((s.sku_id, d, ch, w))
    sku_by_id = {s.sku_id: s for s in skus}

    def expected_orders(w: float, sid: str, d: date) -> float:
        s = sku_by_id[sid]
        eff = apply_effects(idx, sid, d)
        ctr = s.base_ctr * eff.get("ctr", 1.0)
        detail = 0.62
        cart = s.base_cart * eff.get("cart", 1.0)
        pay = s.base_pay * eff.get("cvr", 1.0)
        return w * 6.0 * ctr * detail * cart * pay  # 6.0 = 曝光基数占位，后面统一标定

    total_w = sum(expected_orders(w, sid, d) for sid, d, ch, w in cells)
    scale = orders_target / total_w if total_w > 0 else 0.0

    traffic_rows: list[tuple] = []
    orders_rows: list[tuple] = []
    refund_rows: list[tuple] = []
    review_rows: list[tuple] = []
    ads_rows: list[tuple] = []
    inv_rows: list[tuple] = []
    comp_rows: list[tuple] = []
    agg: dict[tuple[str, str], list[float]] = {}
    order_seq = 0

    comp_by_sku = {s.sku_id: rng.sample(COMPETITORS, k=2) for s in skus}
    rating_shift = {}
    for a in anomalies:
        if "rating" in a.effects and a.skus:
            for sid in a.skus:
                for d in daterange(a.start, a.end):
                    rating_shift[(sid, d)] = a.effects["rating"]

    for sid, d, ch, w in cells:
        s = sku_by_id[sid]
        eff = apply_effects(idx, sid, d)
        impressions = max(1, int(round(w * 6.0 * scale * eff.get("impressions", 1.0) * rng.uniform(0.9, 1.1))))
        clicks = max(0, int(round(impressions * s.base_ctr * eff.get("ctr", 1.0) * jitter(rng, 0.12))))
        detail_uv = max(0, int(round(clicks * 0.62 * jitter(rng, 0.08))))
        add_cart = max(0, int(round(detail_uv * s.base_cart * eff.get("cart", 1.0) * jitter(rng, 0.12))))
        paid_orders = add_cart * s.base_pay * eff.get("cvr", 1.0) * eff.get("orders", 1.0) * jitter(rng, 0.15)
        n_orders = int(paid_orders) + (1 if rng.random() < (paid_orders - int(paid_orders)) else 0)
        n_orders = min(n_orders, add_cart)
        # 买家数 < 订单数（一人多单），用 1.18 单/人近似
        pay_uv = max(1, int(round(n_orders / 1.18))) if n_orders else 0

        discount = rng.uniform(0.82, 1.0)
        if promo_of(d)[0]:
            discount *= 0.90
        unit_price = round(s.price_tag * discount, 2)
        gmv = round(unit_price * n_orders, 2)

        traffic_rows.append((d.isoformat(), sid, ch, impressions, clicks, detail_uv,
                             add_cart, pay_uv, n_orders, gmv))
        key = (d.isoformat(), sid)
        row = agg.setdefault(key, [0, 0, 0, 0, 0, 0, 0.0, 0.0])
        row[0] += impressions; row[1] += clicks; row[2] += detail_uv
        row[3] += add_cart; row[4] += pay_uv; row[5] += n_orders; row[6] += gmv

        # 订单明细
        shipped = d + timedelta(days=rng.randint(1, 3))
        for _ in range(n_orders):
            order_seq += 1
            oid = f"O{d.strftime('%Y%m%d')}{order_seq:07d}"
            seg = s_segment(rng, s)
            qty = 1 if rng.random() < 0.94 else 2
            amount = round(unit_price * qty, 2)
            orders_rows.append((oid, d.isoformat(), sid, ch, seg, qty, unit_price,
                                amount, round(s.price_tag * qty - amount, 2), "finished"))
            # 退款
            p_refund = s.base_refund * eff.get("refund", 1.0)
            if rng.random() < p_refund:
                bias = reason_bias.get((sid, d))
                if bias and rng.random() < REASON_BIAS_RATE:
                    code = bias
                else:
                    code, _, _ = weighted_choice(rng, REFUND_REASONS)
                refund_rows.append((f"R{oid}", oid, (d + timedelta(days=rng.randint(2, 9))).isoformat(),
                                    sid, code, round(amount * rng.uniform(0.85, 1.0), 2), "done"))
            # 评价
            if rng.random() < 0.15:
                r = review_rating(rng, s, eff, rating_shift.get((sid, d), 0.0))
                tag = "尺码" if (not s.size_chart and rng.random() < 0.5) else rng.choice(REVIEW_TAGS)
                size_fit = rng.choice(["偏小", "偏小", "合适", "合适", "偏大"])
                review_rows.append((f"RV{oid}", (d + timedelta(days=rng.randint(3, 12))).isoformat(),
                                    sid, r, size_fit, tag, 1 if rng.random() < 0.18 else 0))

        # 广告
        if s.is_ad and ch in ("CH_REC", "CH_SEARCH"):
            spend = round(gmv * 0.22 * eff.get("spend", 1.0) * rng.uniform(0.8, 1.2) + rng.uniform(3, 8), 2)
            ads_rows.append((d.isoformat(), sid, ch, spend, impressions, clicks, n_orders))
            row[7] += spend

        # 竞品价：(dt, sku, competitor) 唯一，只在首个渠道写一次
        if ch == s.channels[0] and (d.weekday() == 0 or n_orders > 0):
            for c in comp_by_sku[sid]:
                base = s.price_tag * eff.get("competitor_price", 1.0)
                cp = round(base * rng.uniform(0.86, 1.12), 2)
                comp_rows.append((d.isoformat(), sid, c, cp,
                                  1 if promo_of(d)[0] else 0, rng.randint(120, 9800)))

    # 库存：按销量累计扣减，含补货
    stock = {s.sku_id: rng.randint(120, 600) for s in skus}
    daily_sold: dict[tuple[str, str], int] = {}
    for row in traffic_rows:
        daily_sold[(row[0], row[1])] = daily_sold.get((row[0], row[1]), 0) + row[8]
    for d in days:
        for s in skus:
            sold = daily_sold.get((d.isoformat(), s.sku_id), 0)
            inbound = rng.randint(200, 500) if d.weekday() == 0 else 0
            stock[s.sku_id] = stock[s.sku_id] + inbound - sold
            eff = apply_effects(idx, s.sku_id, d)
            out = 1 if (stock[s.sku_id] <= 0 or eff.get("stockout")) else 0
            inv_rows.append((d.isoformat(), s.sku_id, max(0, stock[s.sku_id]), inbound, out))
            if stock[s.sku_id] < 0:
                stock[s.sku_id] = 0

    # 写库
    con.executemany("INSERT INTO fact_traffic_daily VALUES (?,?,?,?,?,?,?,?,?,?)", traffic_rows)
    con.executemany("""INSERT INTO fact_orders
        (order_id, dt, sku_id, channel_id, segment_id, qty, unit_price, pay_amount, discount, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", orders_rows)
    con.executemany("INSERT INTO fact_refunds VALUES (?,?,?,?,?,?,?)", refund_rows)
    con.executemany("INSERT INTO fact_reviews VALUES (?,?,?,?,?,?,?)", review_rows)
    con.executemany("INSERT INTO fact_ads VALUES (?,?,?,?,?,?,?)", ads_rows)
    con.executemany("INSERT INTO fact_competitor_price VALUES (?,?,?,?,?,?)", comp_rows)
    con.executemany("INSERT INTO inventory_snapshot VALUES (?,?,?,?,?)", inv_rows)
    con.executemany("""INSERT INTO agg_daily
        (dt, sku_id, impressions, clicks, detail_uv, add_cart_uv, pay_uv, paid_orders, gmv, ad_spend)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [(k[0], k[1], *v[:6], v[6], round(v[7], 2)) for k, v in agg.items()])
    con.commit()

    return {
        "traffic": len(traffic_rows),
        "orders": len(orders_rows),
        "refunds": len(refund_rows),
        "reviews": len(review_rows),
        "ads": len(ads_rows),
        "competitor_price": len(comp_rows),
        "inventory": len(inv_rows),
        "agg_daily": len(agg),
    }


def s_segment(rng: random.Random, s: Sku) -> str:
    if s.price_band == "低价":
        return rng.choices(["SEG_STU", "SEG_HUNTER", "SEG_FAMILY"], [0.42, 0.38, 0.20])[0]
    if s.price_band == "中价":
        return rng.choices(["SEG_OFFICE", "SEG_COMMUTE", "SEG_HUNTER"], [0.42, 0.38, 0.20])[0]
    return rng.choices(["SEG_OFFICE", "SEG_COMMUTE", "SEG_FAMILY"], [0.45, 0.40, 0.15])[0]


def weighted_choice(rng: random.Random, items):
    total = sum(w for _, _, w in items)
    r = rng.uniform(0, total)
    acc = 0.0
    for it in items:
        acc += it[2]
        if r <= acc:
            return it
    return items[-1]


def review_rating(rng: random.Random, s: Sku, eff: dict[str, float], shift: float) -> int:
    # shift 来自 anomaly.effects["rating"]，是「星级偏移」，负数=口碑变差
    base = 4.42 + shift
    if not s.size_chart:
        base -= 0.05
    if eff.get("neg_review", 1.0) > 1.2:
        base -= 0.35
    r = int(clamp(round(rng.gauss(base, 0.95)), 1, 5))
    return r


# ---------------------------------------------------------------- 素材渲染


def render_images(skus: list[Sku], out_dir: Path, roles=("main",)) -> int:
    from PIL import Image, ImageDraw, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    font_path = next((p for p in [
        r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ] if os.path.exists(p)), None)
    font = ImageFont.truetype(font_path, 44) if font_path else ImageFont.load_default()
    small = ImageFont.truetype(font_path, 26) if font_path else ImageFont.load_default()

    palette = ["#F2E9DD", "#DDE6EA", "#E8DDE6", "#DFE8DD", "#EDE3D3", "#E4E1EC"]
    n = 0
    for idx, s in enumerate(skus):
        for role in roles:
            w = h = 800
            # 用序号而非 hash()：hash() 受 PYTHONHASHSEED 影响，会导致图不可复现
            bg = palette[idx % len(palette)]
            img = Image.new("RGB", (w, h), bg)
            dr = ImageDraw.Draw(img)
            if s.background == "渐变":
                for y in range(h):
                    k = y / h
                    dr.line([(0, y), (w, y)], fill=(int(210 + 30 * k), int(205 + 25 * k), int(200 + 20 * k)))
            elif s.background == "场景":
                dr.rectangle([0, 0, w, int(h * 0.55)], fill=(226, 219, 208))
                dr.rectangle([0, int(h * 0.55), w, h], fill=(205, 190, 178))
            if s.collage:
                dr.rectangle([w // 2, 0, w // 2 + 4, h], fill=(255, 255, 255))
            if s.model_present:
                dr.ellipse([w * 0.36, h * 0.16, w * 0.64, h * 0.44], fill=(232, 214, 200))
                dr.polygon([(w * 0.30, h * 0.42), (w * 0.70, h * 0.42),
                            (w * 0.80, h * 0.95), (w * 0.20, h * 0.95)], fill=(214, 188, 172))
            if s.text_blocks:
                y = 24
                for line in s.text_blocks:
                    dr.rectangle([24, y, 24 + 12 + len(line) * 40, y + 62], fill=(255, 255, 255))
                    dr.text((36, y + 8), line, font=font, fill=(60, 50, 45))
                    y += 74
            if s.size_chart:
                x0, y0 = w - 210, h - 190
                dr.rectangle([x0, y0, w - 20, h - 20], fill=(255, 255, 255), outline=(120, 120, 120))
                dr.text((x0 + 12, y0 + 10), "尺码表", font=small, fill=(40, 40, 40))
                for i in range(3):
                    dr.line([(x0 + 12, y0 + 58 + i * 36), (w - 32, y0 + 58 + i * 36)], fill=(180, 180, 180))
            if s.watermark:
                dr.text((w * 0.60, h * 0.48), "SAMPLE", font=font, fill=(250, 248, 245))
            path = out_dir / f"{s.sku_id}_{role}.png"
            img.save(path, "PNG", optimize=True)
            n += 1
    return n


def material_features(s: Sku, role: str) -> dict:
    flags = []
    if not s.size_chart:
        flags.append("无尺码表")
    if s.text_coverage > 0.35:
        flags.append("文字占比偏高")
    if s.scene == "纯白底":
        flags.append("纯白底无场景")
    if s.watermark:
        flags.append("有水印")
    if s.collage:
        flags.append("拼接图")
    return {
        "sku_id": s.sku_id,
        "role": role,
        "text_blocks": s.text_blocks,
        "text_coverage": s.text_coverage,
        "model_present": s.model_present,
        "scene": s.scene,
        "background": s.background,
        "color_palette": ["#F2E9DD", "#8B6F5C"],
        "size_chart_present": s.size_chart,
        "watermark": s.watermark,
        "collage": s.collage,
        "quality_flags": flags,
        "confidence": 1.0,
        "source": "mock",
    }


# ---------------------------------------------------------------- 其他表


POLICIES = [
    ("POL_001", "商品主图规范", "主图不得出现拼接、水印、边框；文字占比建议不超过 20%；服饰类须在详情页提供尺码表。", "素材,主图"),
    ("POL_002", "虚假宣传处罚", "使用「最」「第一」等绝对化用语，或与实际不符的功效描述，按《广告法》处理。", "合规,宣传"),
    ("POL_003", "价格保护规则", "报名平台活动的商品，活动价需为近 30 天最低价的 9 折以内，且活动期间不得提价。", "活动,价格"),
    ("POL_004", "发货时效要求", "现货商品需在 48 小时内发货，预售商品按详情页承诺时间发货，超时计入物流体验分。", "履约,物流"),
    ("POL_005", "尺码不符退货", "因尺码问题产生的退货，若详情页未提供尺码表，平台倾向支持买家，运费由商家承担。", "售后,尺码"),
    ("POL_006", "双 11 报名门槛", "店铺体验分 ≥ 4.6、近 90 天无严重违规、发货及时率 ≥ 95%。", "活动,双11"),
    ("POL_007", "直播间引流规范", "直播间挂链商品需与讲解商品一致，不得使用「全网最低」等无法验证的比价话术。", "直播,合规"),
    ("POL_008", "评价管理", "不得以任何形式诱导好评或骚扰买家修改评价，违者扣分并限制活动报名。", "评价,合规"),
    ("POL_009", "库存与超卖", "超卖订单需 24 小时内联系买家协商，超时未处理按缺货赔付。", "库存,履约"),
    ("POL_010", "新客券规则", "新客券面额不得超过商品售价的 30%，同一用户 90 天内限领一次。", "营销,优惠券"),
]


def write_store_state(rng: random.Random, con: sqlite3.Connection, skus: list[Sku], anomalies: list[Anomaly]) -> None:
    rows = []
    now = "2026-11-18T09:00:00"
    price_mult: dict[str, float] = {}
    for a in anomalies:
        if "price" in a.effects and a.skus:
            for sid in a.skus:
                price_mult[sid] = a.effects["price"]
    for s in skus:
        price = round(s.price_tag * 0.92 * price_mult.get(s.sku_id, 1.0), 2)
        rows += [
            (STORE_ID, s.sku_id, "price", f"{price:.2f}", now),
            (STORE_ID, s.sku_id, "bid", f"{rng.uniform(0.8, 2.4):.2f}", now),
            (STORE_ID, s.sku_id, "creative", f"v{s.image_version}", now),
            (STORE_ID, s.sku_id, "coupon", "none", now),
        ]
    con.executemany("INSERT INTO store_state VALUES (?,?,?,?,?)", rows)
    con.commit()


def write_gt(con: sqlite3.Connection, anomalies: list[Anomaly]) -> None:
    con.executemany(
        "INSERT INTO anomaly_gt (code, name, dt_start, dt_end, sku_id, root_cause, magnitude, note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(a.code, a.name, a.start.isoformat(), a.end.isoformat(),
          None if a.skus is None else ",".join(sorted(a.skus)), a.root_cause, a.magnitude, a.note)
         for a in anomalies])
    con.commit()


def write_policies(con: sqlite3.Connection) -> None:
    con.executemany("INSERT INTO policy_docs VALUES (?,?,?,?)", POLICIES)
    con.commit()


# ---------------------------------------------------------------- 入口


def build(db_path: Path, sku_count: int, days: int, orders_target: int, seed: int,
          end: date, with_images: bool = True) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    rng = random.Random(seed)
    day_list = build_days(days, end)
    skus = build_skus(rng, sku_count, day_list)
    anomalies = rescale_anomalies(build_anomalies(rng, skus, day_list), day_list)

    con.executemany("INSERT INTO dim_channel VALUES (?,?,?)", CHANNELS)
    con.executemany("INSERT INTO dim_segment VALUES (?,?,?,?,?)", SEGMENTS)
    con.executemany(
        "INSERT INTO dim_date VALUES (?,?,?,?,?,?,?)",
        [(d.isoformat(), d.weekday() + 1, f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}",
          d.strftime("%Y-%m"), promo_of(d)[0], promo_of(d)[1], season_of(d)) for d in day_list])
    con.executemany(
        "INSERT INTO dim_sku VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(s.sku_id, s.title, "女装", "连衣裙", s.price_tag, s.cost, s.launch.isoformat(),
          s.image_version, s.supplier, s.style, s.fabric, s.color, s.price_band) for s in skus])
    con.commit()

    stats = gen_facts(rng, skus, day_list, anomalies, orders_target, con)
    write_gt(con, anomalies)
    write_policies(con)
    write_store_state(rng, con, skus, anomalies)

    img_dir = BASE_DIR / "data" / "images"
    n_img = 0
    if with_images:
        n_img = render_images(skus, img_dir, roles=("main",))
    rows = []
    now = "2026-11-18T09:00:00"
    for s in skus:
        for role in ("main",):
            rows.append((f"{s.sku_id}_{role}", s.sku_id, role,
                         f"data/images/{s.sku_id}_{role}.png",
                         json.dumps(material_features(s, role), ensure_ascii=False), "mock", now))
    con.executemany("INSERT INTO image_feature VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()

    stats["images"] = n_img
    stats["skus"] = len(skus)
    stats["days"] = len(day_list)
    stats["anomalies"] = len(anomalies)
    con.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成店诊 Agent 合成数据")
    ap.add_argument("--db", default=str(BASE_DIR / "data" / "store.db"))
    ap.add_argument("--sku-count", type=int, default=300)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--orders", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--end-date", default="2026-11-18")
    ap.add_argument("--tiny", action="store_true", help="20 SKU / 30 天 / 3000 订单，快速自检")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args(argv)

    sku_count, days, orders = args.sku_count, args.days, args.orders
    if args.tiny:
        sku_count, days, orders = 20, 30, 3000
    end = date.fromisoformat(args.end_date)

    stats = build(Path(args.db), sku_count, days, orders, args.seed, end,
                  with_images=not args.no_images)
    print("seed ok:", json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
