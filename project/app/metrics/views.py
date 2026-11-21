"""语义层视图：每个 source 一个扁平化的只读子查询。

指标 expr 里出现的列名必须来自对应视图。视图统一暴露 `dt` 与一组公共维度列，
时间粒度（week/month/dow/season/is_promo）由编译器 join `dim_date` 得到。
"""

from __future__ import annotations

from typing import Final

_COMMON_DIMS: Final = (
    "sku_id",
    "style",
    "fabric",
    "color",
    "price_band",
    "supplier",
    "category_l1",
    "category_l2",
)

VIEWS: Final[dict[str, str]] = {
    "traffic": """
        SELECT t.dt             AS dt,
               t.sku_id         AS sku_id,
               t.channel_id     AS channel_id,
               s.style          AS style,
               s.fabric         AS fabric,
               s.color          AS color,
               s.price_band     AS price_band,
               s.supplier       AS supplier,
               s.category_l1    AS category_l1,
               s.category_l2    AS category_l2,
               t.impressions    AS impressions,
               t.clicks         AS clicks,
               t.detail_uv      AS detail_uv,
               t.add_cart_uv    AS add_cart_uv,
               t.pay_uv         AS pay_uv,
               t.paid_orders    AS paid_orders,
               t.gmv            AS gmv
          FROM fact_traffic_daily t
          JOIN dim_sku s ON s.sku_id = t.sku_id
    """,
    "orders": """
        SELECT o.dt                          AS dt,
               o.sku_id                      AS sku_id,
               o.channel_id                  AS channel_id,
               o.segment_id                  AS segment_id,
               s.style                       AS style,
               s.fabric                      AS fabric,
               s.color                       AS color,
               s.price_band                  AS price_band,
               s.supplier                    AS supplier,
               s.category_l1                 AS category_l1,
               s.category_l2                 AS category_l2,
               o.qty                         AS qty,
               o.unit_price                  AS unit_price,
               o.pay_amount                  AS pay_amount,
               s.cost                        AS cost,
               CASE WHEN f.refund_id IS NOT NULL THEN 1 ELSE 0 END AS refund_flag,
               f.reason_code                 AS reason_code
          FROM fact_orders o
          LEFT JOIN fact_refunds f ON f.order_id = o.order_id
          JOIN dim_sku s ON s.sku_id = o.sku_id
    """,
    "ads": """
        SELECT a.dt             AS dt,
               a.sku_id         AS sku_id,
               a.channel_id     AS channel_id,
               s.style          AS style,
               s.fabric         AS fabric,
               s.color          AS color,
               s.price_band     AS price_band,
               s.supplier       AS supplier,
               s.category_l1    AS category_l1,
               s.category_l2    AS category_l2,
               a.spend          AS spend,
               a.impressions    AS impressions,
               a.clicks         AS clicks,
               a.conversions    AS conversions,
               COALESCE(t.gmv, 0) AS gmv
          FROM fact_ads a
          LEFT JOIN fact_traffic_daily t
                 ON t.dt = a.dt AND t.sku_id = a.sku_id AND t.channel_id = a.channel_id
          JOIN dim_sku s ON s.sku_id = a.sku_id
    """,
    "reviews": """
        SELECT r.dt          AS dt,
               r.sku_id      AS sku_id,
               s.style       AS style,
               s.fabric      AS fabric,
               s.color       AS color,
               s.price_band  AS price_band,
               s.supplier    AS supplier,
               s.category_l1 AS category_l1,
               s.category_l2 AS category_l2,
               r.rating      AS rating,
               r.size_fit    AS size_fit,
               r.text_tag    AS text_tag
          FROM fact_reviews r
          JOIN dim_sku s ON s.sku_id = r.sku_id
    """,
    "inventory": """
        SELECT i.dt            AS dt,
               i.sku_id        AS sku_id,
               s.style         AS style,
               s.fabric        AS fabric,
               s.color         AS color,
               s.price_band    AS price_band,
               s.supplier      AS supplier,
               s.category_l1   AS category_l1,
               s.category_l2   AS category_l2,
               i.stock_qty     AS stock_qty,
               i.inbound_qty   AS inbound_qty,
               i.stockout_flag AS stockout_flag
          FROM inventory_snapshot i
          JOIN dim_sku s ON s.sku_id = i.sku_id
    """,
}

# 每个视图允许的维度列（dt 与时间粒度维度全局可用）
VIEW_COLUMNS: Final[dict[str, set[str]]] = {
    "traffic": {"dt", "sku_id", "channel_id", *_COMMON_DIMS},
    "orders": {"dt", "sku_id", "channel_id", "segment_id", *_COMMON_DIMS},
    "ads": {"dt", "sku_id", "channel_id", *_COMMON_DIMS},
    "reviews": {"dt", "sku_id", *_COMMON_DIMS},
    "inventory": {"dt", "sku_id", *_COMMON_DIMS},
}

# 每个视图的度量列：只有指标 expr 能用，不能当维度分组
VIEW_MEASURES: Final[dict[str, set[str]]] = {
    "traffic": {"impressions", "clicks", "detail_uv", "add_cart_uv", "pay_uv", "paid_orders", "gmv"},
    "orders": {"qty", "unit_price", "pay_amount", "cost", "refund_flag", "reason_code"},
    "ads": {"spend", "impressions", "clicks", "conversions", "gmv"},
    "reviews": {"rating", "size_fit", "text_tag"},
    "inventory": {"stock_qty", "inbound_qty", "stockout_flag"},
}

# 时间维度 -> SQL 表达式（都来自 dim_date 别名 d）
TIME_DIMS: Final[dict[str, str]] = {
    "dt": "v.dt",
    "week": "d.week",
    "month": "d.month",
    "dow": "d.dow",
    "season": "d.season",
    "is_promo": "d.is_promo",
}
