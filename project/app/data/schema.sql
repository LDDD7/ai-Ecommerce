-- 店诊 Agent 数据层 schema（SQLite 3.49+，WAL）
-- 生成物，可随时 `python -m app.data.seed` 重建。不要手改。

PRAGMA foreign_keys = OFF;

-- ---------- 维度 ----------
CREATE TABLE IF NOT EXISTS dim_date (
    dt          TEXT PRIMARY KEY,          -- YYYY-MM-DD
    dow         INTEGER NOT NULL,          -- 1=周一
    week        TEXT NOT NULL,             -- 2026-W21
    month       TEXT NOT NULL,             -- 2026-05
    is_promo    INTEGER NOT NULL DEFAULT 0,
    promo_name  TEXT,
    season      TEXT NOT NULL              -- 春/夏/秋/冬
);

CREATE TABLE IF NOT EXISTS dim_sku (
    sku_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    category_l1   TEXT NOT NULL,
    category_l2   TEXT NOT NULL,
    price_tag     REAL NOT NULL,           -- 标价
    cost          REAL NOT NULL,           -- 成本（含履约）
    launch_date   TEXT NOT NULL,
    image_version INTEGER NOT NULL,        -- 主图版本，换代 +1
    supplier      TEXT NOT NULL,
    style         TEXT NOT NULL,           -- 通勤/度假/甜美/...
    fabric        TEXT NOT NULL,           -- 雪纺/棉/真丝/...
    color         TEXT NOT NULL,
    price_band    TEXT NOT NULL            -- 低价/中价/高价
);

CREATE TABLE IF NOT EXISTS dim_channel (
    channel_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL             -- 推荐/搜索/直播/私域/活动
);

CREATE TABLE IF NOT EXISTS dim_segment (
    segment_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    age_band         TEXT NOT NULL,
    price_sensitivity TEXT NOT NULL,       -- 低/中/高
    city_tier        TEXT NOT NULL
);

-- ---------- 事实 ----------
CREATE TABLE IF NOT EXISTS fact_traffic_daily (
    dt           TEXT NOT NULL,
    sku_id       TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    impressions  INTEGER NOT NULL,
    clicks       INTEGER NOT NULL,
    detail_uv    INTEGER NOT NULL,
    add_cart_uv  INTEGER NOT NULL,
    pay_uv       INTEGER NOT NULL,
    paid_orders  INTEGER NOT NULL,
    gmv          REAL NOT NULL,
    PRIMARY KEY (dt, sku_id, channel_id)
);

CREATE TABLE IF NOT EXISTS fact_orders (
    order_id     TEXT PRIMARY KEY,
    dt           TEXT NOT NULL,
    sku_id       TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    segment_id   TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    unit_price   REAL NOT NULL,
    pay_amount   REAL NOT NULL,
    discount     REAL NOT NULL,
    status       TEXT NOT NULL             -- paid/shipped/finished/refunded
);

CREATE TABLE IF NOT EXISTS fact_refunds (
    refund_id    TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL,
    dt           TEXT NOT NULL,
    sku_id       TEXT NOT NULL,
    reason_code  TEXT NOT NULL,            -- 尺码偏小/色差/质量/物流慢/不喜欢
    amount       REAL NOT NULL,
    status       TEXT NOT NULL             -- pending/done
);

CREATE TABLE IF NOT EXISTS fact_ads (
    dt          TEXT NOT NULL,
    sku_id      TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    spend       REAL NOT NULL,
    impressions INTEGER NOT NULL,
    clicks      INTEGER NOT NULL,
    conversions INTEGER NOT NULL,
    PRIMARY KEY (dt, sku_id, channel_id)
);

CREATE TABLE IF NOT EXISTS fact_reviews (
    review_id  TEXT PRIMARY KEY,
    dt         TEXT NOT NULL,
    sku_id     TEXT NOT NULL,
    rating     INTEGER NOT NULL,           -- 1..5
    size_fit   TEXT NOT NULL,              -- 偏小/合适/偏大
    text_tag   TEXT NOT NULL,              -- 尺码/色差/质量/物流/好评
    has_image  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_competitor_price (
    dt          TEXT NOT NULL,
    sku_id      TEXT NOT NULL,
    competitor  TEXT NOT NULL,
    price       REAL NOT NULL,
    promo_flag  INTEGER NOT NULL,
    rating_cnt  INTEGER NOT NULL,
    PRIMARY KEY (dt, sku_id, competitor)
);

CREATE TABLE IF NOT EXISTS inventory_snapshot (
    dt           TEXT NOT NULL,
    sku_id       TEXT NOT NULL,
    stock_qty    INTEGER NOT NULL,
    inbound_qty  INTEGER NOT NULL,
    stockout_flag INTEGER NOT NULL,
    PRIMARY KEY (dt, sku_id)
);

-- ---------- 多模态素材 ----------
CREATE TABLE IF NOT EXISTS image_feature (
    image_id      TEXT PRIMARY KEY,
    sku_id        TEXT NOT NULL,
    role          TEXT NOT NULL,            -- main/detail/scene
    path          TEXT NOT NULL,            -- data/images/xxx.png
    features_json TEXT NOT NULL,            -- MaterialFeatures
    source        TEXT NOT NULL,            -- mock/vlm
    created_at    TEXT NOT NULL
);

-- ---------- 业务状态与日志 ----------
CREATE TABLE IF NOT EXISTS store_state (
    store_id    TEXT NOT NULL,
    sku_id      TEXT NOT NULL,
    field       TEXT NOT NULL,              -- price/bid/creative/coupon/stock
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (store_id, sku_id, field)
);

CREATE TABLE IF NOT EXISTS action_log (
    action_id       TEXT PRIMARY KEY,
    trace_id        TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    type            TEXT NOT NULL,
    sku_id          TEXT,
    payload_json    TEXT NOT NULL,
    risk_level      INTEGER NOT NULL,
    status          TEXT NOT NULL,          -- drafted/approved/executed/rolled_back/rejected
    idempotency_key TEXT NOT NULL UNIQUE,
    approver        TEXT,
    executed_at     TEXT,
    rollback_of     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id   TEXT,
    event       TEXT NOT NULL,              -- drafted/approved/rejected/executed/rolled_back
    actor       TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anomaly_gt (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,              -- R1..R10
    name        TEXT NOT NULL,
    dt_start    TEXT NOT NULL,
    dt_end      TEXT NOT NULL,
    sku_id      TEXT,                       -- NULL = 全店
    root_cause  TEXT NOT NULL,
    magnitude   TEXT NOT NULL,              -- 人类可读，如 "cvr -18%"
    note        TEXT
);

CREATE TABLE IF NOT EXISTS policy_docs (
    doc_id  TEXT PRIMARY KEY,
    title   TEXT NOT NULL,
    body    TEXT NOT NULL,
    tags    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace (
    trace_id     TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    response    TEXT NOT NULL,
    tokens      INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_run (
    run_id       TEXT PRIMARY KEY,
    cases        INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    baseline     TEXT,
    created_at   TEXT NOT NULL
);

-- ---------- 预聚合（P95 兜底） ----------
CREATE TABLE IF NOT EXISTS agg_daily (
    dt          TEXT NOT NULL,
    sku_id      TEXT NOT NULL,
    impressions INTEGER NOT NULL,
    clicks      INTEGER NOT NULL,
    detail_uv   INTEGER NOT NULL,
    add_cart_uv INTEGER NOT NULL,
    pay_uv      INTEGER NOT NULL,
    paid_orders INTEGER NOT NULL,
    gmv         REAL NOT NULL,
    ad_spend    REAL NOT NULL,
    PRIMARY KEY (dt, sku_id)
);

-- ---------- 索引 ----------
CREATE INDEX IF NOT EXISTS idx_traffic_sku_dt   ON fact_traffic_daily (sku_id, dt);
CREATE INDEX IF NOT EXISTS idx_traffic_dt       ON fact_traffic_daily (dt);
CREATE INDEX IF NOT EXISTS idx_orders_dt        ON fact_orders (dt);
CREATE INDEX IF NOT EXISTS idx_orders_sku       ON fact_orders (sku_id, dt);
CREATE INDEX IF NOT EXISTS idx_refunds_dt       ON fact_refunds (dt);
CREATE INDEX IF NOT EXISTS idx_refunds_sku      ON fact_refunds (sku_id, dt);
CREATE INDEX IF NOT EXISTS idx_reviews_sku_dt   ON fact_reviews (sku_id, dt);
CREATE INDEX IF NOT EXISTS idx_ads_dt           ON fact_ads (dt);
CREATE INDEX IF NOT EXISTS idx_comp_sku_dt      ON fact_competitor_price (sku_id, dt);
CREATE INDEX IF NOT EXISTS idx_inv_sku_dt       ON inventory_snapshot (sku_id, dt);
CREATE INDEX IF NOT EXISTS idx_img_sku          ON image_feature (sku_id);
CREATE INDEX IF NOT EXISTS idx_action_trace     ON action_log (trace_id);
CREATE INDEX IF NOT EXISTS idx_gt_window        ON anomaly_gt (dt_start, dt_end);

-- ---------------------------------------------------------------------------
-- 检索（spec §20）：向量只存 SQLite BLOB，全文用 FTS5 trigram，不引向量库
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embedding (
    owner_table TEXT NOT NULL,          -- policy_docs / playbook
    owner_id    TEXT NOT NULL,          -- doc_id / card_id
    model       TEXT NOT NULL,          -- 例如 BAAI/bge-m3
    dim         INTEGER NOT NULL,
    vec         BLOB NOT NULL,          -- float32 小端、已归一化
    text_hash   TEXT NOT NULL,          -- sha256(model|title|body)，变了才重算
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (owner_table, owner_id, model)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    owner_table UNINDEXED,
    owner_id    UNINDEXED,
    body,
    tokenize = 'trigram'
);

