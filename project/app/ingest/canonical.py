"""规范模型：把各平台的导出字段归一到一组固定字段。

适配器只负责「列名 → 规范字段」的翻译，之后所有计算都只认这里定义的字段。
新增一个平台 = 在 `aliases` 里补几个列名，不需要改这里以外的代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Field:
    """一个规范字段：名称、中文标签、类型、是否必需、常见列名。"""

    name: str
    label: str
    kind: str = "text"  # text / int / money / date
    required: bool = False
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    label: str
    note: str
    fields: tuple[Field, ...]
    #: 至少要命中的一个字段，否则这份数据「认出来了也没用」。
    #: 例：流量表只有日期和商品、没有任何曝光/点击/成交列时，不该被当成流量数据。
    signals: tuple[str, ...] = ()

    @property
    def fields_by_name(self) -> dict[str, Field]:
        return {f.name: f for f in self.fields}


ORDERS = Dataset(
    name="orders",
    label="订单",
    note="一行一个订单商品行；同一订单多商品时按行展开即可",
    fields=(
        Field("order_id", "订单号", required=True,
              aliases=("订单号", "订单编号", "订单id", "orderid", "orderno", "order",
                       "name", "主订单编号", "平台订单号", "交易订单号")),
        Field("dt", "下单日期", kind="date", required=True,
              aliases=("下单时间", "下单日期", "支付时间", "付款时间", "createdat", "created at",
                       "订单创建时间", "成交时间", "日期")),
        Field("sku_id", "商品编码", required=True,
              aliases=("商品编码", "商家编码", "商品id", "sku", "skuid", "sku编码",
                       "lineitemsku", "货号", "宝贝id", "商品编号")),
        Field("qty", "数量", kind="int", aliases=("数量", "购买数量", "件数", "商品数量",
                                                "lineitemquantity", "qty", "num")),
        Field("pay_amount", "实付金额", kind="money", required=True,
              aliases=("买家实付金额", "实付金额", "支付金额", "成交金额", "实收款",
                       "total", "totalsales", "ordertotal", "金额", "应付金额")),
        Field("discount", "优惠金额", kind="money",
              aliases=("优惠金额", "折扣金额", "优惠", "discount", "卖家优惠", "店铺优惠")),
        Field("unit_price", "单价", kind="money",
              aliases=("单价", "商品单价", "原价", "price", "售价")),
        Field("status", "订单状态", aliases=("订单状态", "状态", "status", "交易状态")),
        Field("channel", "渠道", aliases=("渠道", "来源", "流量来源", "channel", "平台")),
        Field("segment", "人群", aliases=("人群", "客户群", "segment", "新老客")),
    ),
)

TRAFFIC = Dataset(
    name="traffic",
    label="流量",
    note="一行一天一个商品的流量；有曝光/点击才能做漏斗与流量类根因",
    fields=(
        Field("dt", "日期", kind="date", required=True,
              aliases=("日期", "统计日期", "dt", "date", "时间")),
        Field("sku_id", "商品编码", required=True,
              aliases=("商品编码", "商家编码", "商品id", "sku", "skuid", "宝贝id", "货号")),
        Field("impressions", "曝光量", kind="int",
              aliases=("展现量", "曝光量", "展现次数", "impressions", "曝光次数",
                       "曝光人数", "展现人数", "曝光pv", "商品曝光")),
        Field("clicks", "点击量", kind="int",
              aliases=("点击量", "点击次数", "clicks", "点击人数", "点击uv", "商品点击")),
        Field("detail_uv", "详情页访客", kind="int",
              aliases=("访客数", "详情页访客", "uv", "detailuv", "商品访客数", "商品详情页访客数",
                       "详情页uv")),
        Field("add_cart_uv", "加购数", kind="int",
              aliases=("加购数", "加购件数", "加购人数", "addcartuv", "cartuv")),
        Field("pay_uv", "支付买家数", kind="int",
              aliases=("支付买家数", "支付人数", "payuv", "成交买家数", "购买人数")),
        Field("paid_orders", "支付订单数", kind="int",
              aliases=("支付订单数", "订单数", "paidorders", "成交订单数", "支付子订单数")),
        Field("gmv", "成交金额", kind="money",
              aliases=("支付金额", "成交金额", "gmv", "支付子订单金额")),
        Field("channel", "渠道", aliases=("渠道", "来源", "channel", "平台")),
    ),
    signals=("impressions", "clicks", "detail_uv", "add_cart_uv", "pay_uv", "paid_orders", "gmv"),
)

PRODUCTS = Dataset(
    name="products",
    label="商品",
    note="商品基础信息；缺了也能跑，只是维度下钻会变粗",
    fields=(
        Field("sku_id", "商品编码", required=True,
              aliases=("商品编码", "商家编码", "商品id", "sku", "skuid", "宝贝id", "货号")),
        Field("title", "商品标题", required=True,
              aliases=("商品标题", "商品名称", "标题", "title", "宝贝标题", "productname")),
        Field("category_l1", "一级类目", aliases=("一级类目", "类目", "分类", "category")),
        Field("category_l2", "二级类目", aliases=("二级类目", "子类目", "subcategory")),
        Field("price_tag", "吊牌价", kind="money", aliases=("吊牌价", "标价", "原价", "price")),
        Field("cost", "成本", kind="money", aliases=("成本", "成本价", "cost")),
        Field("style", "款式", aliases=("款式", "风格", "style")),
        Field("fabric", "面料", aliases=("面料", "材质", "fabric")),
        Field("color", "颜色", aliases=("颜色", "色系", "color")),
        Field("launch_date", "上架日期", kind="date", aliases=("上架日期", "上架时间", "launchdate")),
    ),
)

REFUNDS = Dataset(
    name="refunds",
    label="退款",
    note="退款单；有它才能算退款率、区分尺码与物流类根因",
    fields=(
        Field("refund_id", "退款单号", aliases=("退款单号", "退款编号", "refundid", "售后单号")),
        Field("order_id", "订单号", required=True,
              aliases=("订单号", "订单编号", "orderid", "orderno", "交易订单号")),
        Field("dt", "退款日期", kind="date", required=True,
              aliases=("退款时间", "退款日期", "申请时间", "日期")),
        Field("sku_id", "商品编码", aliases=("商品编码", "商家编码", "sku", "skuid")),
        Field("amount", "退款金额", kind="money",
              aliases=("退款金额", "退款额", "amount", "refundamount")),
        Field("reason", "退款原因", aliases=("退款原因", "原因", "reason", "售后原因")),
        Field("status", "退款状态", aliases=("退款状态", "状态", "status")),
    ),
    signals=("amount", "reason", "refund_id"),
)

DATASETS: dict[str, Dataset] = {
    d.name: d for d in (ORDERS, TRAFFIC, PRODUCTS, REFUNDS)
}

#: 有能力读到这些表，就能算对应的指标（用于告诉商家「缺什么」）。
CAPABILITIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("orders", "订单", ("gmv", "paid_orders", "aov", "unit_price", "buyers")),
    ("traffic", "流量", ("impressions", "clicks", "ctr", "uv", "cvr", "detail_uv", "add_cart_uv")),
    ("refunds", "退款", ("refund_rate", "refund_amount", "return_rate")),
    ("products", "商品", ("品类/款式/价格带下钻", "同行素材对比")),
    ("reviews", "评价", ("rating", "neg_review_rate", "dsr")),
    ("inventory", "库存", ("stockout_rate", "stock_qty")),
    ("ads", "广告", ("roi", "cpc", "ad_spend")),
)

_DATE_PATTERNS = (
    re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)


def normalize_header(text: Any) -> str:
    """列名归一：去空格、全角转半角、去括号里的单位与冒号。"""
    raw = str(text or "").strip()
    raw = raw.replace("（", "(").replace("）", ")")
    raw = re.sub(r"\([^)]*\)", "", raw)
    raw = raw.replace("：", "").replace(":", "")
    raw = raw.replace("￥", "").replace("¥", "").replace("元", "")
    return re.sub(r"\s+", "", raw).lower()


def parse_date(value: Any) -> str | None:
    """把各种写法的日期解析成 YYYY-MM-DD；认不出返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(int(value))
    elif hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    else:
        text = str(value).strip()
    if text == "":
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            year, month, day = (int(g) for g in match.groups())
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def parse_money(value: Any) -> float | None:
    """金额解析：容忍 ¥、千分位、正负号、括号负数。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if text == "":
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[¥￥$,，\s元]", "", text)
    text = text.replace("−", "-")
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative and number > 0 else number


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = re.sub(r"[,\s件个单]", "", str(value).strip())
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


PARSERS = {"date": parse_date, "money": parse_money, "int": parse_int, "text": lambda v: str(v).strip()}


def coerce(dataset: Dataset, mapping: dict[str, str], raw_row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按映射把一行原始数据转成规范行；返回 (规范行, 错误列表)。"""
    row: dict[str, Any] = {}
    errors: list[str] = []
    for field in dataset.fields:
        column = mapping.get(field.name)
        raw = raw_row.get(column) if column else None
        if raw is None or str(raw).strip() == "":
            if field.required:
                errors.append(f"{field.label}为空")
            continue
        value = PARSERS[field.kind](raw)
        if value is None:
            if field.required:
                errors.append(f"{field.label}无法解析为{field.kind}")
            continue
        row[field.name] = value
    return row, errors


def canonical_payload() -> dict[str, Any]:
    """给前端「数据导入」页用的数据字典：四个数据集、字段、别名、口径说明。"""
    return {
        "datasets": [
            {
                "name": dataset.name,
                "label": dataset.label,
                "note": dataset.note,
                "fields": [
                    {
                        "name": field.name,
                        "label": field.label,
                        "kind": field.kind,
                        "required": field.required,
                        "aliases": list(field.aliases),
                    }
                    for field in dataset.fields
                ],
            }
            for dataset in DATASETS.values()
        ],
        "capabilities": [
            {"dataset": name, "label": label, "metrics": list(metrics)}
            for name, label, metrics in CAPABILITIES
        ],
        "accepts": [".csv", ".xlsx"],
        "max_bytes": 50 * 1024 * 1024,
    }
