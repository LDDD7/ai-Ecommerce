# 数据模型与指标口径

> 这份文档回答：**库里有什么表、每个指标怎么算、维度怎么切、金额和时间是什么口径。**
> 接口层怎么用见 [api.md](./api.md)，为什么这么设计见 [architecture.md](./architecture.md)。

## 1. 总览

SQLite 单文件（`project/data/store.db`，WAL 模式，约 82MB）。分三层：

```
事实表（每天每 SKU 一行/一单）     维表（人/货/场）        系统表（运行状态）
fact_orders         196,228        dim_sku        303      users / sessions
fact_traffic_daily  127,095        dim_channel      6      app_setting
fact_refunds         22,712        dim_date       194      llm_cache / vision_cache
fact_ads             15,120        dim_segment      5      ingest_run / ingest_undo
fact_reviews         29,414                               action_log / audit_log
inventory_snapshot   54,000                               creative_draft / promo_calendar
fact_competitor_price 79,508                              anomaly_gt / eval_run
agg_daily            54,000                              search_fts（FTS5）
```

时间范围：`2026-05-23` ~ `2026-11-18`（180 天，`dim_date` 194 行含前后几天）。

## 2. 事实表

### 2.1 `fact_orders`（主键 `order_id`）

| 列 | 类型 | 说明 |
|---|---|---|
| `order_id` | TEXT PK | 订单号（导入时来自平台） |
| `dt` | TEXT | 下单日期 `YYYY-MM-DD` |
| `sku_id` | TEXT | 商品 |
| `channel_id` | TEXT | 渠道（`CH_TB/CH_DY/CH_JD/CH_PDD/CH_SITE/CH_OTHER`） |
| `segment_id` | TEXT | 人群段 |
| `qty` | INTEGER | 件数 |
| `unit_price` | REAL | 件单价（元） |
| `pay_amount` | REAL | 实付金额（元，= `unit_price*qty - discount`） |
| `discount` | REAL | 优惠金额 |
| `status` | TEXT | `paid/refunded/...` |

### 2.2 `fact_traffic_daily`（主键 `dt + sku_id + channel_id`）

`impressions / clicks / detail_uv / add_cart_uv / pay_uv / paid_orders / gmv`。
注意 `paid_orders` 与 `fact_orders` 是两条独立口径（一个来自平台流量后台，一个来自订单明细），诊断里**每个指标只认自己的来源表**，不混用。

### 2.3 其它事实表

| 表 | 主键 | 关键列 |
|---|---|---|
| `fact_refunds` | `refund_id` | `order_id, dt, sku_id, reason_code, amount, status` |
| `fact_ads` | `dt + sku_id + channel_id` | `spend, clicks, gmv` |
| `fact_reviews` | `review_id` | `dt, sku_id, rating(1-5), size_fit, text_tag` |
| `inventory_snapshot` | `dt + sku_id` | `stock, stockout_flag, days_of_stock` |
| `fact_competitor_price` | `dt + sku_id + competitor` | `price`（竞品最低价，R1 用） |

## 3. 维表

| 表 | 主键 | 列 |
|---|---|---|
| `dim_sku` | `sku_id` | `title, category_l1/l2, price_tag, cost, launch_date, image_version, supplier, style, fabric, color, price_band` |
| `dim_channel` | `channel_id` | `name, type` |
| `dim_date` | `dt` | `dow, week, month, is_promo, promo_name, season` |
| `dim_segment` | `segment_id` | `name, description` |

`price_band` 分档：`0-99 / 100-199 / 200-499 / 500+`（导入新 SKU 时按 `price_tag` 自动落档）。

## 4. 系统表

| 表 | 用途 |
|---|---|
| `users` / `sessions` | 账号（scrypt 哈希）与 Cookie 会话（只存 `sha256(token)`） |
| `app_setting` | 设置页的覆盖值（空值 = 删除覆盖，回到 `.env`） |
| `llm_cache` / `vision_cache` | 模型结果缓存（键含模型名与 prompt 版本；视觉另含图片 sha256） |
| `ingest_run` / `ingest_undo` | 导入批次（新增/变更/无变化/状态）与撤销用的改前值（zlib） |
| `action_log` / `audit_log` | 动作状态机与审计事件（`drafted/approved/rejected/executed/rolled_back`） |
| `creative_draft` | 创意工单草稿（图路径、文案、合规、改法） |
| `promo_calendar` | 商家声明的促销期（`rebuild_flags` 写回 `dim_date`） |
| `anomaly_gt` | **注入根因真值**（评测专用，运行时任何代码都不准读它） |
| `search_fts(+_data/_idx/_content/_config/_docsize)` | FTS5 trigram 索引（策略卡 27 + 政策 10） |

`anomaly_gt` 是最容易犯错的地方：**诊断代码读它就等于作弊**。它只允许出现在 `tools/`、`tests/`、`eval/` 里。

## 5. 五个只读视图

`app/metrics/views.py` 定义，编译器只在这些视图上拼 SQL：

| 视图 | 来源 | 可用度量 | 可用维度 |
|---|---|---|---|
| `traffic` | `fact_traffic_daily` + `dim_sku` + `dim_date` | uv/pv/impressions/clicks/ctr/cvr/detail_uv/add_cart_uv/add_cart_rate/pay_buyers | common + channel |
| `orders` | `fact_orders` + `dim_sku` + `dim_date` + `dim_channel` + `dim_segment` | gmv/paid_orders/aov/unit_price/gross_margin/refund_rate/return_rate | 全部四组 |
| `ads` | `fact_ads` | ad_spend/roi/cpc | common + channel |
| `reviews` | `fact_reviews` | neg_review_rate/dsr/review_cnt | common（`sku_only` 子集） |
| `inventory` | `inventory_snapshot` | stockout_rate | common |

## 6. 24 个指标（`app/metrics/catalog.yaml`）

| 指标 | 中文 | 单位 | 来源 | 口径 |
|---|---|---|---|---|
| `uv` | 访客数 | count | traffic | `SUM(detail_uv)` |
| `pv` | 浏览量 | count | traffic | `SUM(clicks)` |
| `impressions` | 曝光量 | count | traffic | `SUM(impressions)` |
| `clicks` | 点击量 | count | traffic | `SUM(clicks)` |
| `ctr` | 点击率 | ratio | traffic | `clicks / impressions` |
| `cvr` | 支付转化率 | ratio | traffic | `paid_orders / detail_uv` |
| `detail_uv` | 详情页 UV | count | traffic | `SUM(detail_uv)` |
| `add_cart_uv` | 加购 UV | count | traffic | `SUM(add_cart_uv)` |
| `add_cart_rate` | 加购率 | ratio | traffic | `add_cart_uv / detail_uv` |
| `pay_buyers` | 支付买家数 | count | traffic | `SUM(pay_uv)` |
| `gmv` | 成交金额 | currency | orders | `SUM(pay_amount)` |
| `paid_orders` | 支付订单数 | count | orders | `COUNT(*)` |
| `aov` | 客单价 | currency | orders | `gmv / 订单数` |
| `unit_price` | 件单价 | currency | orders | `AVG(unit_price)` |
| `gross_margin` | 毛利率 | ratio | orders | `(gmv - cost*qty) / gmv` |
| `refund_rate` | 退款率 | ratio | orders | 退款订单 / 订单数 |
| `return_rate` | 尺码退货率 | ratio | orders | `reason_code='SIZE_SMALL'` 占比 |
| `ad_spend` | 投放花费 | currency | ads | `SUM(spend)` |
| `roi` | 投放 ROI | ratio | ads | `gmv / spend` |
| `cpc` | 平均点击成本 | currency | ads | `spend / clicks` |
| `neg_review_rate` | 差评率 | ratio | reviews | `rating<=2` 占比 |
| `dsr` | 平均评分 | score | reviews | `AVG(rating)` |
| `review_cnt` | 评价数 | count | reviews | `COUNT(*)` |
| `stockout_rate` | 断货率 | ratio | inventory | `AVG(stockout_flag)` |

比率指标是「先汇总再相除」，不是「每行比率再平均」——这是能让 `gmv / 订单数` 与逐行相除对不上的那类错误，也是为什么编译层统一用 `SUM/SUM`。

## 7. 维度组

| 组 | 字段 |
|---|---|
| `common` | `dt, week, month, dow, season, is_promo, sku_id, style, fabric, color, price_band, supplier, category_l1, category_l2` |
| `channel` | `channel_id` |
| `segment` | `segment_id` |
| `sku_only` | `dt, week, month, sku_id, style, price_band, color, fabric, supplier` |

每个指标声明自己能切哪几组，编译器据此拒绝非法组合（例如给 `reviews` 加 `channel_id` 会 400）。

## 8. 口径约定

1. **金额**：元，REAL，两位小数。展示时前端再缩写（`¥101.4 万`）。
2. **比率**：0~1 小数，展示乘 100。比较时用百分点差（`-9.1%` 是相对变化，`+7.9%` 同理）。
3. **时间**：诊断窗口是「含首尾」的 `window_days` 天；基线是窗口前 28 天（等长切分做 z 检验）。
4. **缺数**：`NULLIF` 防除零，比率算不出来返回 `null` 而不是 0——前端显示「—」，诊断层把「算不出来」当证据不足处理。
5. **促销**：`dim_date.is_promo` 只由 `promo_calendar` 派生（导入不写它）；`promo_skew` = 窗口促销日占比 − 基线占比。
6. **`anomaly_gt`**：10 条注入根因（`code, sku_id/style, dt_start, dt_end`），只用于评测。评测取真值是「与诊断窗口有交集」，不是「as_of 落在窗口内」——后者会把刚结束的根因漏掉。

## 9. 合成数据怎么来的

`app/data/generator.py`（seed 固定 42）：

1. 生成 300 个 SKU、6 个渠道、5 个人群段、194 天日期维；
2. 按「基础流量 × 季节 × 促销 × 趋势 × 噪声」生成日级流量与转化，再展开成订单；
3. 注入 10 类根因（每类 1 条，写 `anomaly_gt`），让对应 SKU/款式在指定窗口出现该有的形态（断货→`stockout_rate` 跳升、差评→`neg_review_rate` 跳升…）；
4. 生成 300 张主图 + `image_feature`（属性即真值，`tools/rerender_images.py` 会把图画成与属性一致的样子）。

重新生成会覆盖 `data/store.db`，**先备份**：

```bash
python -m app.data.seed          # 生成（约 40s）
python -m tools.rerender_images  # 重画主图（约 30s，可选）
```
