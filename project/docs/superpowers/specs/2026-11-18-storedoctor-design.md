# 店诊 Agent（StoreDoctor）设计说明

> 日期：2026-11-18
> 状态：待评审
> 周期：3 周（2026-11-19 ~ 2026-12-09），15–20 h/周
> 定位：电商店铺经营诊断与执行 Agent —— Diagnose-to-Action，不是 ChatBI，也不是第 4 个 RAG 问答。

---

## 1. 背景与目标

### 1.1 要解决的问题

大促期间运营每天在 5 个后台之间翻数：生意参谋看流量、广告后台看投放、客服系统看差评、ERP 看库存、竞品靠手工比价。诊断靠经验，动作靠人，复盘靠回忆。

店诊 Agent 把这条链路收进一个入口：

```
商家提问 → 查数 → 归因（带证据）→ 推演 → 策略 → 人工审批 → dry-run 执行 → 审计 → 复盘
```

### 1.2 为什么不是「再做一个 RAG 知识库」

现有三个项目（病案问数 / 金融投研 / K12 顾问助手）已经覆盖了检索、口径、评测、人审。招聘市场缺的三块是：电商业务域知识、Agent 真执行（调工具改状态、可审批可回滚）、生产级工程（并发、超时、降级、成本、评测门禁）。本项目只补这三块。

一句话对照：病案问数解决「看见」，店诊 Agent 解决「看懂 + 动手」。

### 1.3 成功标准

1. 面试官能 `docker compose up` 一键起，5 分钟内看懂「诊断 → 审批 → 执行」；
2. 每个结论都能点开证据（指标 / 维度值 / 时间窗 / 变化量 / 贡献占比）；
3. 60 条评测任务真跑出数，数字可复现，且能解释「怎么判定算对」；
4. 拔掉 LLM 仍能出模板化诊断报告（降级路径可演示）。

---

## 2. 范围

### 2.1 做

- 合成数据生成器（可复现、注入已知根因、带 ground truth）
- 指标语义层（YAML 定义 + QuerySpec 编译器 + 受控 SQL）
- 安全网关（只读、白名单、行数上限、超时、租户隔离、PII 脱敏、查询审计）
- 多步归因（异常检测 / 恒等式分解 / 维度贡献度 / 漏斗 / 证据链 / Critic）
- 策略库（27 张策略卡 + 混合检索 + 三级重排 + 影响预估）
- 执行闭环（Action schema / dry-run / 分级审批 / 幂等 / mock 执行 / 审计 / 回滚）
- 编排（Planner → Executor → Critic → Reporter）+ LLM 网关（路由 / 重试 / 缓存 / 计量 / 降级）
- **素材诊断**（VLM 抽主图/详情页属性 → 变成证据 → 触发换素材动作；含竞品主图属性对比）
- 单页 Web（React + Vite + TS）：对话、诊断卡、审批台、审计与 trace
- 评测（60 条任务 + 10 条素材诊断、消融、回归门禁）+ 50 并发压测
- Docker 打包、seed、eval 脚本、README、demo 脚本
- **数据接入设计**（adapter 契约、T+1 增量、幂等 upsert、质量校验、多租户与 PII 脱敏，见 §21；本期只出设计与 CSV 导入路径，不接真实平台）

### 2.2 不做

- 不接真实商家数据、不接真实平台 API（数据为合成，执行全部 mock，写入 `store_state`）；但保留 adapter 接入设计（§21），换源不改语义层
- 不做多租户 SaaS 化（只做 store_id 级隔离的演示）
- 不做多 Agent 角色扮演（一个显式状态机足够）
- 不做向量检索（没有可靠 embedding 依赖时，用规则 + 关键词召回 + LLM 重排，不谎称 RAG）
- **不训练/微调 VLM，不做生图（商品图/模特图生成），不做多模态向量检索，不做视频理解** —— 3 周做不出可信结果，只做「用视觉做诊断证据」这一层
- 不做自动无人运营（审批闸门是产品特性，不是妥协）

### 2.3 可调参数（默认值，改动成本 < 30 分钟）

| 参数 | 默认 | 备选 |
|---|---|---|
| 类目 | 女装 · 连衣裙，单店 300 SKU | 家居 / 3C |
| 数据规模 | 20 万订单 / 180 天 | 50 万订单 |
| 注入根因 | 10 类 | 可增删 |

选女装的理由：季节性强、尺码退货率高、竞品比价激烈、素材衰减明显 —— 10 类根因都能自然注入。选 20 万而非 200 万：SQLite 上 P95 仍能压到秒级，生成与复现快。

---

## 3. 关键决策与理由

| # | 决策 | 理由 | 代价 |
|---|---|---|---|
| D1 | 查询引擎用 **SQLite**，不用 DuckDB | venv 无 duckdb，PyPI 直连不稳；SQLite 支持窗口函数与 CTE，20 万行足够 | 少了 OLAP 叙事；语义层做了引擎抽象，后续可切 DuckDB |
| D2 | 前端 **React 18.3.1 + Vite 7.3.8 + TypeScript 5.9.3** | 市场要求最多（大厂 60–70%、npm 周下载 3000 万 vs Vue 700 万）；AI 应用岗要求流式对话 UI；关键词对齐。钉版本不用 latest（Vite 8 换 Rolldown、TS 7 是 Go 重写版，均太新） | 多约 1–1.5 天；构建链进 Docker |
| D3 | **API-first**：后端先定 `/api/*` 契约，UI 只做消费者 | 前端若延期，可直接退回 Jinja2 服务端渲染，后端零改动 | 需维护一份接口契约文档 |
| D4 | **不引 UI 组件库**，样式手写，图表自写 SVG | AntD 等会吃掉时间；SVG 离线可跑，Docker 里不白屏 | 组件复用要自己写 |
| D5 | 合成数据 + **注入已知根因** | 归因需要 ground truth 才能评测；比拿一份不知真假的报表更有说服力 | 生成器要写 2 天 |
| D6 | 编排用**显式状态机**，不用 LangGraph | 可控、可测、可解释；面试讲取舍比报框架名有用 | 少一个框架关键词 |
| D7 | 执行全部 **mock**，写入 `store_state` | 无真实 API；但 dry-run / 审批 / 幂等 / 回滚 / 复盘全链路真实 | 不能吹「接了平台 API」 |
| D8 | LLM 走 **OpenAI 兼容网关**，base_url/model 可配 | 不绑厂商；key 由使用者填 `.env` | 需处理不同模型的能力差异 |
| D9 | 多模态只做**素材诊断**（`vision.inspect` → 证据 → `swap_creative`），不做生图/不训模型 | JD 显示电商 AI 岗的多模态集中在「商品理解 + 竞品对比 + 素材生成」；诊断 Agent 的落点是前两个。DeepSeek `deepseek-flash` 支持 OpenAI 兼容图片输入，同一 key，单图 ≤1024 tokens | +1.5 天；无 key 时要能回放 |
| D10 | 检索用 **FTS5(trigram) + 本地 bge-m3 + RRF 融合**，重排分**规则 / cross-encoder / LLM** 三级；**不引向量库**（不用 chromadb/Milvus） | 语料只有 37 条（10 条平台规则 + 27 张策略卡），向量库的运维与依赖成本远超收益；37×1024×4 ≈ 148KB，直接存 SQLite BLOB + numpy 余弦即可。本机已有 `E:\llm\hfmodel\bge-m3`（1024 维，单查询 0.17s）与 `bge-reranker-v2-m3`（0.32s/对），离线可用、零 API 成本 | reranker 常驻 1.9GB 内存、冷启动 19s，必须懒加载 + 降级 |
| D11 | **Critic 先规则后模型**：规则版（七项检查、纯函数）常态开启，LLM 反方审查默认关闭、单张卡最多压 20% 置信度 | 规则版把总体归因精度从 33% 提到 53%、top-3 从 4/6 保持 67%，代价只有几十毫秒；模型版单独跑一次要 2.5s 且倾向于把卡全压成「存疑」，排序失去意义。所以模型的价值定在「提反方意见」而不是「改分数」 | 需要维护两套口径；`?critic=llm` 演示时端到端 5.4s |

---

## 4. 架构

```
[Web: React SPA]  对话 / 诊断卡 / 审批台 / 审计 / trace
      │  HTTP + SSE
[FastAPI]  /api/chat  /api/actions/*  /api/trace/*  /api/audit
      │
[Orchestrator]  显式状态机: Planner → Executor(tool loop) → Critic → Reporter
      │                    │
      │              [Memory] 店铺画像 / 历史诊断 / 已执行动作 / 会话
      │
[Tool Registry]（MCP 风格：name / schema / handler / timeout / idempotent）
   ├─ metrics.query        指标查询（QuerySpec → 受控 SQL）
   ├─ metrics.drilldown    维度贡献度下钻
   ├─ funnel.analyze       曝光→点击→商详→加购→支付
   ├─ competitor.compare   竞品价格 / 评价 / 素材（含视觉属性差异表）
   ├─ vision.inspect       图片 → 结构化素材属性（VLM，可离线回放）
   ├─ policy.search        平台规则 / 活动玩法（FTS5 + 向量混合召回）
   ├─ playbook.search      27 张策略卡（召回 + 重排）
   ├─ action.draft/execute 动作草稿 / 执行（mock）
   └─ report.render        报告渲染
      │
[Retrieval]     FTS5(trigram) + bge-m3(1024d, 本地) → RRF(k=60) → 规则 / cross-encoder / LLM 三级重排
[Semantic Layer]  metric YAML → QuerySpec → SQL compiler → Guard
      │
[Data] SQLite（SQLAlchemy Core + 只读连接），合成数据 + 注入根因 + ground truth
[LLM Gateway] 路由 / 超时 / 重试 / 缓存 / token 计量 / 降级
[Trace] 每次任务一条 JSON：计划、工具入参出参、耗时、token、结论证据
```

分层原则：`app/semantic` 只管「指标怎么算」，`app/diagnose` 只管「异常怎么归因」，`app/actions` 只管「动作怎么安全地落地」，三层互不 import 对方的内部实现，只通过 pydantic 模型通信。

---

## 5. 数据模型

SQLite 单文件 `data/store.db`，WAL 模式；查询走只读连接。

### 5.1 表

| 表 | 粒度 | 关键字段 |
|---|---|---|
| `dim_date` | 天 | dt, week, month, is_promo, promo_name, season |
| `dim_sku` | SKU | sku_id, title, category_l1/l2, price_tag, cost, launch_date, image_version, supplier |
| `dim_channel` | 渠道 | channel_id, name（推荐/搜索/直播/私域/活动）, type |
| `dim_segment` | 人群 | segment_id, name, age_band, price_sensitivity, city_tier |
| `fact_traffic_daily` | 天×SKU×渠道 | impressions, clicks, detail_uv, add_cart_uv, pay_uv, paid_orders, gmv |
| `fact_orders` | 订单 | order_id, dt, sku_id, channel_id, segment_id, qty, unit_price, pay_amount, discount, status |
| `fact_refunds` | 退款单 | refund_id, order_id, dt, sku_id, reason_code, amount, status |
| `fact_ads` | 天×SKU×渠道 | spend, impressions, clicks, conversions |
| `fact_reviews` | 评价 | review_id, dt, sku_id, rating, size_fit, text_tag, has_image |
| `fact_competitor_price` | 天×SKU×竞品 | competitor, price, promo_flag |
| `image_feature` | 图 | image_id, sku_id, role(主图/详情页/场景), features_json, source(vlm/mock), created_at |
| `inventory_snapshot` | 天×SKU | stock_qty, inbound_qty, stockout_flag |
| `action_log` | 动作 | action_id, trace_id, type, payload, status, idempotency_key, approver, executed_at, rollback_of |
| `store_state` | 店×SKU×字段 | field（price/bid/creative/coupon/stock）, value, updated_at |
| `anomaly_gt` | 异常窗口 | dt_start, dt_end, sku_id, root_cause, magnitude, note（**ground truth**） |
| `policy_docs` | 文档 | doc_id, title, body, tags |
| `trace` | 任务 | trace_id, task_id, payload_json, created_at |
| `llm_cache` | 缓存 | key, response, tokens, created_at |
| `eval_run` | 评测批次 | run_id, cases, metrics_json, baseline, created_at |

### 5.2 生成器

`app/data/generator.py`：纯 stdlib（不用 Faker，避开额外依赖）+ 业务规则 + 固定随机种子（`--seed 42`），可复现。

1. 先造维度（SKU / 渠道 / 人群 / 日历，含 618、双 11、换季）；
2. 按「基准曲线 × 渠道系数 × 周内效应 × 季节性」造日粒度流量；
3. 由流量按各环节转化率造行为与订单，价格、折扣、人群分布按规则生成；
4. 叠加异常：在指定窗口把指定指标按指定幅度偏移；
5. 写入 `anomaly_gt`（根因、幅度、窗口），供评测对答案。

### 5.3 十类注入根因

| # | 根因 | 主要表现 | 关键证据 |
|---|---|---|---|
| 1 | 推荐位掉量 | 曝光骤降、点击率不变 | channel=推荐 的 impressions 贡献度 |
| 2 | 竞品降价 | 点击正常、商详到支付转化下滑 | competitor_price 价差 + cvr delta |
| 3 | 断货 | 曝光点击正常、支付骤降、库存 0 | stockout_flag + pay_uv 漏斗断点 |
| 4 | 差评/尺码 | 退款率上升、转化下滑 | neg_review_rate + refund reason=尺码 |
| 5 | 退款政策变化 | 退款率跳变、客单不变 | refund_rate delta 集中在某时间点 |
| 6 | 素材衰退 | 点击率缓慢下滑 | ctr 趋势斜率 + 主图版本 |
| 7 | 人群偏移 | 新客占比变化、AOV 变化 | segment 贡献度 + aov delta |
| 8 | 活动结束 | 流量与转化同时回落 | is_promo 切换 + 大盘对照 |
| 9 | 物流延迟 | 退款/差评上升、转化下滑 | 物流时效 + dsr |
| 10 | 价格带迁移 | 类目价格带整体下移 | 价格带分布对比 |

每类至少注入 2 个实例（不同 SKU、不同时间窗），共 20+ 异常窗口；其中 25 条用于诊断评测。

---

## 6. 指标语义层

### 6.1 YAML schema

```yaml
# app/semantic/metrics/gmv.yaml
name: gmv
display: 成交金额
expr: "SUM(fact_traffic_daily.gmv)"
source: fact_traffic_daily
dimensions: [dt, sku_id, channel_id]
grain: [day, week, month]
filters:
  allowed: [channel_id, sku_id, category_l2, is_promo]
owner: 交易组
version: 3
```

约束：`expr` 只允许聚合函数 + 白名单列；`dimensions` 声明可下钻维度；`filters.allowed` 声明可过滤字段；`version` 进审计。

### 6.2 首批 23 个指标

成交：`gmv`、`paid_orders`、`pay_buyers`、`aov`、`unit_price`、`gross_margin`
流量：`uv`、`pv`、`impressions`、`clicks`、`ctr`、`cvr`
行为：`detail_uv`、`add_cart_uv`、`add_cart_rate`
售后：`refund_rate`、`return_rate`、`neg_review_rate`
投放：`ad_spend`、`roi`、`cpc`
供给：`stockout_rate`
服务：`dsr`

（实际计数 23，覆盖文档要求的 15–30 区间；每个都可下钻、可对比、可缓存。）

### 6.3 QuerySpec

```json
{
  "metric": "cvr",
  "dims": ["dt", "channel_id"],
  "filters": [{"field": "sku_id", "op": "in", "value": ["SKU0007"]}],
  "time_range": {"start": "2026-11-10", "end": "2026-11-17", "grain": "day"},
  "compare": {"type": "wow"},
  "order_by": [{"field": "dt", "dir": "asc"}],
  "limit": 200
}
```

编译器 `app/semantic/compiler.py`：校验指标存在 → 校验维度和过滤字段在白名单 → 拼参数化 SQL（绑定变量，不拼字符串）→ 加 `LIMIT` → 返回 `(sql, params)`。

### 6.4 安全网关 `app/semantic/guard.py`

- 只读连接（`file:store.db?mode=ro`）+ 只读事务；
- SQL 必须 `SELECT`，AST 级校验（`sqlglot` 不可用时用正则 + 关键字黑名单 + 单语句校验）；
- 表白名单、字段白名单、禁止子查询套子查询；
- 行数上限 5000、查询超时 8s（`set_progress_handler`）；
- store_id 强制拼接，跨店查询直接拒绝；
- PII：手机号/地址脱敏；
- 审计：谁、何时、什么指标、扫了多少行、SQL 指纹。

### 6.5 自然语言查数（`app/query/nl.py`）

「查数」在早期版本里一直是**后端有、前端没有**的缺口：语义层与 `/api/query` 从 W1 就在跑，但界面上只有一个 disabled 的按钮。补这个缺口有两条路，两条都做了：

| 路径 | 入口 | 成本 | 适用 |
|---|---|---|---|
| 结构化构建 | 前端 `views/Query.tsx` 直接拼 `QuerySpec` | 0 token | 知道指标名的人，最快 |
| 自然语言 | `POST /api/query/nl` → `parse_question` | 有 key 时约 ¥0.002/次 | 不想记指标名的场景 |

**两条路最终都产出同一个 `QuerySpec`，再交给语义层编译器生成参数化 SQL。** 模型没有写 SQL 的机会——这是 §6.4 安全网关能成立的前提；`parse_question` 的返回值也要再过一遍 `QuerySpec` 校验，非法就退到规则解析。

规则解析复用编排层的意图抽取（`app/orchestrator/intents.py`：时间窗、指标、款式、渠道、SKU），再加一层维度词表：

- 维度词：按天/每天 → `dt`，按周 → `week`，按月 → `month`，款式 → `style`，渠道 → `channel_id`，商品/爆款 → `sku_id`，价格带/面料/颜色/供应商/促销日同理；
- 显式窗口优先：「近 14 天」→ 14，「最近一个月」→ 30，「这季度」→ 90，否则用调用方给的默认 7 天；
- 指标识别：先按 intents 的关键词表，再补一层「指标中文名直接出现在问题里」的兜底（如「客单价」→ `aov`）；一个都没识别出来时退回「成交额 + 支付订单数」并在 `warnings` 里说清楚。

LLM 解析把 24 个指标名与 15 个维度喂给模型，要求只输出 `{"metrics":[],"dimensions":[],"window_days":7,"filters":[],"limit":200}`。模型输出一律走白名单过滤与夹紧（指标 ≤6、维度 ≤3、过滤 ≤8、窗口 ≤90 天、行数 ≤5000）；解析失败、返回非对象、校验不过，全部回退规则解析，并在 `warnings` 里保留原因。

接口：

```
POST /api/query/nl
{ "question": "近 14 天成交额和转化率按天看", "as_of": "2026-11-17", "window_days": 7, "use_llm": true }

→ { "spec": {...}, "source": "llm|rule", "warnings": [...], "elapsed_ms": 812.4,
    "as_of": "2026-11-17", "result": { "columns": [...], "rows": [...], "meta": {...} },
    "meta": { "wall_ms": 830.1 } }
```

`source` 是给人看的可信度信号：`llm` 表示模型解析、`rule` 表示关键词回退；前端据此显示「模型解析 / 规则解析」徽标，而不是假装两者一样可靠。

实测（`data/store.db`，as_of=2026-11-17）：规则解析「近 14 天成交额和转化率按天看」→ `gmv,cvr` × `dt` × 14 行，31ms；「甜美款近 7 天退款率」→ 带 `style=甜美` 过滤、单行；「最近一个月各款式客单价」→ `aov` × `style` × 30 天、5 行。16 项离线测试覆盖规则窗口/过滤/维度、LLM 成功、未知指标过滤、维度上限、窗口夹紧、异常回退、以及 `/api/query/nl` 的成功与两个 400。

---

---

### 6.6 过滤器编辑器（前端）

查数页的 ilters 不是只读的：自然语言解析出的条件会摊回编辑器，可以继续改。

- 数据结构：编辑器用 FilterRow{id, field, op, value:string}，提交前 
owsToFilters() 转成 QueryFilter（in/
ot in 按逗号拆数组，etween 必须两个值，其余按字符串），iltersToRows() 负责反向。
- 字段选项取「已选指标可分组维度的交集」，已经出现在某行的字段会被补进选项，避免自然语言带来的字段在下拉里消失。
- 操作符按字段类型分两套：时间字段（dt）用 = / >= / <= / etween，其余用 = / != / in / 
ot in / like；换字段时若两边类型不同自动回落到 =。
- 有未填完的行（没选字段、值为空、etween 不足两个值）时，运行查询 不发送请求，行内红字说明原因——避免把 400 当成「没有数据」。
- 上限取目录的 limits.max_filters（当前 8），到上限后「添加条件」置灰。


## 7. 归因引擎

`app/diagnose/`：

1. **异常检测** `anomaly.py`：与「上周同一天 × 前 7 天中位数」比较，输出 `deviation`、`z_score`、`is_anomaly`（阈值可配）。
2. **恒等式分解** `decompose.py`：`GMV = UV × CVR × AOV`；`refund_rate = refund_orders / paid_orders`；逐因子算贡献（对数分解或逐步替代法，实现择一并在 README 写明）。
3. **维度贡献度** `contribution.py`：`ΔM = Σ_i (M_i¹ − M_i⁰)`，按渠道 / SKU / 人群 / 价格带 / 时间分摊，输出占比。
4. **漏斗** `funnel.py`：曝光 → 点击 → 商详 → 加购 → 支付，定位断点及该环节转化率变化。
5. **证据链** `evidence.py`：`Evidence{metric, dim_field, dim_value, window, baseline, current, delta, contribution_pct}`；**缺证据不允许出结论**（Reporter 校验，缺字段直接降级为「数据不足」）。
6. **根因假设** `hypothesis.py`：规则映射（证据模式 → 候选根因），按贡献度排序取 top-3，LLM 只负责选下一步下钻维度与写解释。
7. **Critic** `critic.py`：检查结论与证据是否矛盾、是否把相关说成因果、是否漏掉大盘因素；不通过则打回重算或降级。

---

## 8. 策略库

`app/playbook/cards/*.yaml`，27 张卡（原计划 30 张，砍 3 张非核心卡，为素材诊断腾 1.5 天）：

```yaml
id: pb_price_match
name: 竞品降价跟进
applies_when:
  - metric: cvr
    delta_pp: "<= -0.5"
  - evidence: competitor_price_gap
    op: ">="
    value: 0.05
action_template:
  type: adjust_price
  params: {direction: down, pct: 0.03}
expected_impact: {gmv_pct: "+2~4", margin_pct: "-1~2"}
cost: 低
risk_level: 2
counterexample: 若库存不足 3 天，降价只会加速断货，应先补货
tags: [价格, 转化]
```

- 检索：**混合召回 → 三级重排**（详见 §20）。规则过滤（`root_cause` / `metric` / `risk` 硬条件）→ FTS5 + bge-m3 各取 top-12 → RRF 融合 → 规则分排序，或可选 cross-encoder 精排；返回 top-5 并附 `score_breakdown`，能回答「为什么不是另一张卡」；
- 兜底：top-1 相关性低于阈值（cross-encoder 分 <0.2，或规则分 <1.0）时不硬凑，输出「策略库无匹配卡，建议人工介入」，并记入评测的「可执行性」扣分项；
- 影响预估：价格弹性系数 + 历史同类动作回归，输出保守区间，标注置信度。

---

## 9. 执行闭环

- **Action Schema**：`type`（adjust_price / adjust_bid / swap_creative / issue_coupon / restock_alert）、`target`（store/sku）、`params`、`budget`、`effective_at`、`reason`、`trace_id`。
- **dry-run**：算影响（预估 GMV / 毛利 / 风险），不落库，返回 diff。
- **分级审批**：risk 0 自动（补货提醒）、risk 1 单人（换主图、小额券）、risk 2 双人/店长（改价 >5%、加投 >20%）。
- **幂等**：`sha256(type + store_id + sku_id + date + params_hash)` 唯一索引，重复提交返回既有结果，不重复执行。
- **执行**：mock API 更新 `store_state` 并写 `action_log`，状态机 `draft → approved → executed → rolled_back`。
- **回滚**：反向动作引用 `rollback_of`，走同一条审计链路。
- **审计**：操作人、审批人、时间、trace_id、前后值 diff。
- **复盘**：3 天后对比预期 vs 实际（合成数据里直接读执行后窗口），更新策略卡权重。

---

### 9.1 实现口径（T8）

三块代码，职责不重叠：

| 文件 | 职责 | 不做什么 |
|---|---|---|
| `app/actions/schema.py` | 五类动作的参数契约、风险分级、幂等键、护栏 | 不碰数据库 |
| `app/actions/plan.py` | 从诊断卡 + 策略卡动作生成草案（取值、算幅度、跳过原因） | 不算风险（交给 schema） |
| `app/actions/store.py` | 落库、状态机、审批、审计、trace 聚合 | 不算参数 |
| `app/actions/execute.py` | mock 执行（写 `store_state`）、dry-run 预览、回滚 | 不判权限 |

**表**：`action_log` 沿用 §5.1 的定义；新增 `audit_log(id, action_id, event, actor, detail_json, created_at)`，老库由 `store.ensure_tables()` 用 `CREATE TABLE IF NOT EXISTS` 补齐（`schema.sql` 里也有）。

**五类动作 → 存储字段**：

| 动作 | 必填参数 | 写哪个 `store_state.field` | 默认风险 |
|---|---|---|---|
| `adjust_price` | `new_price` | `price` | 1（变动 ≤5% 且涨幅 <10%），否则 2 |
| `adjust_bid` | `bid`（+`budget_delta`/`plan_id`） | `bid` | 1，涨幅 >20% 或加投 >20% → 2 |
| `swap_creative` | `creative_id` | `creative` | 1 |
| `issue_coupon` | `amount`（+`threshold`/`quota`/`budget`） | `coupon`（JSON） | 1，面额 >50 或总额 >2000 → 2 |
| `restock_alert` | 无（可选 `days_of_stock`/`supplier`） | `stock`（JSON） | 0（自动） |

**状态机**（`schema.TRANSITIONS`，越界一律 `ActionError`）：

```
drafted ──approve──▶ approved ──execute──▶ executed ──rollback──▶ rolled_back
   │                    │
   ├──reject──▶ rejected└──reject──▶ rejected
   └──execute（**只有 risk 0 允许跳过审批**）
```

风险 2 的 `approve` 要求 `role ∈ {manager, owner}`，否则 400；状态机不认识的迁移（例如把 `rejected` 再 approve、把 `rolled_back` 再 execute）直接拒绝。

**幂等**：`sha256(type|store_id|sku_id|day|params_json)` 作为 `action_log.idempotency_key` 唯一索引。重复提交返回既有动作并带 `duplicate: true`，**不新建也不重复执行**。日期进键是有意的：今天没补货，明天当然可以再提醒。

**护栏**（`check_guardrails`，mock 执行也要有，否则一次误判能改 300 个 SKU）：降幅/涨幅 >30% 直接拒；调价后毛利率 <5% 拒绝；出价涨幅 >50% 拒绝；券面额 >100 元或总预算 >5000 元拒绝（后者在参数校验层）。变动 ≥5% 时返回警告文案，随动作一起入库。

**mock 执行**：`execute.apply()` 只做一件事——按动作类型算 `next_value()` 写 `store_state`，并把**旧值**存进 `payload.snapshot`。回滚时按快照恢复：原本没有该字段就删掉。全店级动作只允许 `restock_alert`，其余必须给 `sku_id`（防止一次改全店）。

**审计**：每次状态迁移写一行 `audit_log`，`detail_json` 里含参数、风险、审批角色、前后值 diff；`GET /api/trace/{trace_id}` 把同一 trace 的动作与审计事件按时间聚合。

**接口**（均已实现）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/actions/types` | 五类动作的参数与风险说明（前端渲染表单） |
| POST | `/api/actions/dry-run` | 预览 before/after 与副作用，**不写库** |
| POST | `/api/actions` | 新建草案（幂等） |
| POST | `/api/actions/draft` | 从诊断卡 + 策略库批量起草（演示/脚本用） |
| GET | `/api/actions?status=&type=&limit=` | 列表：`items` 受 limit 截断，`count` 是筛选命中总数，`total` 是全量总数，`status_counts` 跟随 `type` 过滤且不受 limit 截断 |
| GET | `/api/actions/{id}` | 详情（含 `params/snapshot/diff`） |
| POST | `/api/actions/{id}/approve` | 审批（risk 2 需 manager/owner） |
| POST | `/api/actions/{id}/reject` | 驳回 |
| POST | `/api/actions/{id}/execute` | mock 执行 |
| POST | `/api/actions/{id}/rollback` | 按快照回滚 |
| GET | `/api/audit?action_id=&limit=` | 审计流水 |
| GET | `/api/trace/{trace_id}` | 动作 + 审计聚合（前端「Trace」视图直接消费） |

**实测（真库 `data/store.db`，`adjust_price` SKU0001 148 → 142）**：dry-run 返回 `before=null / after=142.00`；create → `drafted, risk 1`；未审批 execute → 400「该动作风险等级 1（需人审），必须先审批再执行」；approve → `approved`；execute → `executed`，`diff={before:null, after:"142.00"}`；重复 create → `duplicate: true`；rollback → `rolled_back`，`store_state.price` 被删除（回到原状）；`audit` 4 条、`trace` 4 条。

### 9.2 编排接线（T8 第二步）

动作闭环不是只有 API，它还要能在「问一句」里自然出现：诊断出根因 → 配到策略卡 → **当场起草案**，用户看到的是一句话结论加一串待审批动作，而不是先去翻接口文档。

**工具层**（`app/orchestrator/tools.py`）

| 工具 | 依赖 | 行为 | 输出 |
|---|---|---|---|
| `action.draft` | `diagnose.run` 的 cards + `playbook.search` 的 items | 调 `app.actions.plan.draft_actions`，每卡最多 2 条、整批最多 6 条 | `{items:[动作…], meta:{created, duplicate, cards, skipped, elapsed_ms}}`，summary「起草 N 条动作（低风险 x / 需人审 y / 高风险 z）」 |

缺依赖时返回 `ok=False` 与「依赖诊断结果，需要先运行 diagnose.run」，不抛异常——编排层把它变成 `error` 事件，一轮对话其余部分照常产出。

**计划层**（`app/orchestrator/planner.py`）

- `RulePlanner` 的诊断意图固定五步：`insights.overview → diagnose.run → diagnose.decompose → playbook.search → action.draft`。
- `LLMPlanner` 的白名单 `TOOL_CATALOG` 增加 `action.draft`；`DEPENDS_ON` 增加 `action.draft → playbook.search`（模型只给起草、不给配动作时会自动插前置）；`MAX_LLM_STEPS` 由 4 提到 5。

**执行层**（`app/orchestrator/runner.py`）

- `_Ctx` 增加 `trace_id`（`tr_xxxxxxxx`），起草动作时写进 `action_log`，同一个 `trace_id` 才能把「诊断 → 动作 → 审计」串起来，供 `GET /api/trace/{id}` 回放。
- 新分支发 `action_draft` 事件：`{status:"ok", store_id, items, meta}`；`done` 增加 `actions`（起草条数）与 `trace_id`。
- `compose(...)` 多接一个 `actions` 参数，报告多一段「已起草为待审批动作（在「执行」页确认后生效）」，每条写「动作 → risk 等级 → 依据哪张卡」；有 skipped 时补一行「另有 N 条没起草：<原因>」。

**实测（`project/.tmp/t8_e2e.py`，真库副本，`as_of=2026-11-17`，`LLM_MODE=mock`）**

- 事件：plan 1 / tool_call 5 / tool_result 5 / diagnosis 5 / evidence 8 / critic 1 / playbook 1 / action_draft 1 / token 84 / done 1。
- 五步耗时：overview 375ms → diagnose 1835ms（含第二轮复审）→ decompose 0ms（复用）→ playbook 201ms → action.draft 112ms；端到端 **2534ms**。
- 起草结果：6 条（低风险 2 / 需人审 4 / 高风险 0），例如 `SKU0292 补货预警`(risk 0)、`SKU0292 换素材 SKU0292-v2`(risk 1)、`SKU0184 发 ¥7 券（预算 ¥700）`(risk 1)；`meta.created=6, duplicate=0, cards=4`。
- 报告尾部出现「建议动作（按诊断卡）：」与「已起草为待审批动作（在「执行」页确认后生效）：」两段。

**已知边界**

1. `swap_creative` 的版本号按 `image_feature` 行数 +1 生成，删过图行会重号；长期应改为 `max(version)+1`。
2. 券预算缺省按 `amount × quota` 估（`quota` 默认 100），金额小的券也会算出几百元预算，展示上要带上 quota 一起看。
3. 起草只处理 `scope.dim == "sku_id"` 的卡；全店级/款式级卡只在报告里建议，不自动起草案（怕一次改动 300 个 SKU）。
4. 前端目前只消费 `action_draft` 事件里的数量与 meta，动作清单的审批界面见 §9.3。

### 9.3 执行台（T9 前端）

**目标**：把「Agent 起草了什么」变成「人能看懂、能批准、能撤」的一张台面；只调 `/api/actions*`，不引入路由库与状态库。

**组件**

| 文件 | 职责 |
|---|---|
| `web/src/api.ts`（动作块） | `ActionItem/ActionList/AuditList/TracePayload/ActionPreview` 类型与 11 个调用函数；`listActions` 返回 `count/total/status_counts` 三个计数 |
| `web/src/views/Approval.tsx` | 列表页：状态胶囊（全部/待审批/已批准/已执行/已回滚/已驳回）+ 类型下拉 + 「共 N 条 · 待审 M 条」；行内 chip（risk 2/1/0 → high/medium/low）、摘要、`类型 · 商品 · 状态`、时间与警告数；骨架屏、空态、错误重试；请求用递增序号防竞态 |
| `web/src/views/ActionDetail.tsx` | 右侧抽屉：元信息、参数、基线、`before → after` 差异、预期效果、警告、审计时间线；按状态显示「批准/驳回/执行/回滚」；Esc 与点遮罩关闭；操作成功用返回的动作对象就地更新并通知父级刷新 |
| `web/src/styles-actions.css` | `ax-*` / `axd-*` 样式：延续浅色令牌，列表行按状态给 3px 左竖条，抽屉 560px 宽、sticky 操作条、`@supports` 下 `body:has(.axd){overflow:hidden}`，`prefers-reduced-motion` 关动效 |

**交互规则**

1. `risk_level == 0` 的动作允许 `drafted → executed` 直接执行；其余必须先 `approve`，否则后端 400「必须先审批再执行」。
2. 已 `rejected` / `rolled_back` 的动作不再显示操作按钮，只显示「已结束」。
3. 抽屉里任何成功操作都触发父级 `refreshKey + 1`，列表重新拉取（计数、状态徽标同步）。
4. 审批人默认「店长」、角色默认 `manager`（risk 2 才校验 manager/owner）。

**接口计数语义**：`items` 受 `limit` 截断；`count` 是当前 `status`+`type` 过滤下的命中总数；`total` 是不带过滤的全量条数（「全部」胶囊用它）；`status_counts` 跟随 `type` 过滤、但不受 `limit` 影响。

**验收（实测，真库 + 真 HTTP）**

- 诊断 2026-11-17 → 4 张卡（R7 甜美、R7 度假、R3 SKU0292、R3 SKU0270）→ 起草 4 条动作（2 条 risk 0 补货、2 条 risk 1 换素材）。
- 未审批执行 → 400「该动作风险等级 1（需人审），必须先审批再执行」；risk 0 直接执行 → 200 `executed`。
- 批准（店长/manager）→ 执行（`diff.field=creative`，`before=null → after=SKU0292-v2`）→ 回滚（`reverted=true`，回到 `null`）→ 重复回滚 400。
- 审计 4 条（`rolled_back / executed / approved / drafted`，倒序）；`trace/tr_t9http` 聚合 7 条事件；`GET /api/actions` 5ms。
- 前端：`tsc --noEmit` 0 错误；`scripts/ui_check.mjs` 0 缺失（2 个未使用类为预留）。

## 10. 编排与 LLM 网关

- 状态机：`parse_intent → plan → tool_loop → diagnose → playbook → draft_action → critic → report`，每步产出一条 trace 事件；任一步失败可降级。
- 工具注册：`ToolSpec{name, description, json_schema, handler, timeout_s, idempotent}`，统一超时与重试，工具异常返回结构化错误而不是抛栈。
- LLM 网关 `app/core/llm.py`：OpenAI 兼容；小模型做意图/槽位抽取，大模型做规划/解释；超时（默认 20s）+ 重试（2 次，指数退避）+ 结果缓存 + token 计量；失败降级到模板报告。
- 上下文裁剪：只带当前诊断相关的证据与最近 6 轮对话；策略卡只带 top-5。

---

## 11. 前端

React 18 + Vite + TypeScript，构建产物由 FastAPI 静态托管（`web/dist`）。

### 11.1 视图（单页多标签）

原计划四个视图，实际交付八个（多出来的都是「让结论能被查证、被动作化」必需的）：

1. **体检台**：健康分、成交额恒等式、转化漏斗、告警、诊断卡摘要；
2. **诊断与证据**：诊断卡（结论 + 证据表 + Critic 反方意见 + 素材块）+ 漏斗 + 贡献度；
3. **问诊**：消息流 + SSE 流式，页脚显示本次用的规划器与报告来源（模型改写 / 模板）；
4. **查数**：结构化勾选（指标 / 维度 / 过滤条件 / 时间窗）+ 自然语言两种入口，都能看到将要执行的 QuerySpec；
5. **数据导入**：上传 CSV/XLSX → 识别数据集与字段 → 质量报告 → 提交前差异确认 → 入库 / 按批次撤销；
6. **执行台**：动作列表（状态胶囊 + 计数）+ 审批抽屉（dry-run 差异、审计时间线、审批 / 执行 / 回滚）；
7. **Trace**：按 `trace_id` 聚合一条链路上的全部动作与审计事件（概览 + 时间线 + 动作表 + 折叠的原始返回），从执行台抽屉「查看 trace」进入；
8. **设置**：BYO-key（LLM 与生图），保存即生效、只回掩码。

审计类文案（状态、事件、动作类型、参数名、风险、角色、时间格式）统一收在 `web/src/audit-labels.ts`，执行台、动作抽屉、Trace 三处共用，界面里不出现英文标识或 JSON。

### 11.2 SSE 协议

`POST /api/chat` 返回 `text/event-stream`，事件类型：`plan`、`tool_call`、`tool_result`、`evidence`、`diagnosis`、`playbook`、`action_draft`、`token`（文本增量）、`done`、`error`。前端 `useChatStream` 用 `fetch` + `ReadableStream` 解析（不用 EventSource，因为要 POST）。

### 11.3 图表

自写 SVG 组件：`LineChart`（趋势）、`BarChart`（贡献度）、`Funnel`（漏斗）。无第三方图表库。

### 11.4 构建

`web/` 独立 npm 工程；`npm run build` 产出 `web/dist`；开发时 Vite proxy 到 `:8000`。

---

## 12. 评测

### 12.1 任务集（70 条）

| 类型 | 数量 | 判定 |
|---|---|---|
| 问数 | 20 | 指标 / 维度 / 时间窗 / 过滤**四项全对**才算对（不看算出来的数） |
| 诊断 | 25 | 根因 top-3 命中（`anomaly_gt`）、证据完整率、可执行性 1–5 分 |
| 动作 | 15 | 类型正确率、参数合规率、预估误差 |
| 素材诊断（`vision` suite） | 10 | 属性抽取字段准确率、素材证据完整率、竞品差异表字段齐全；单列计分，不混进主基线 |

### 12.2 消融

裸 SQL vs 语义层；无 Critic vs 有（已实测：6 个候选日、24→19 张卡、精度 33%→53%、top-3 4/6=67%，见 §22.5）；单 Agent（一次规划）vs 规划+执行两段；**检索/重排**：`fts` / `fts+vec` / `fts+vec+cross` / `fts+vec+cross+llm` 四档，看策略卡 top-3 命中率与 P95 延迟的边际收益。

### 12.3 门禁

`python -m app.eval.run --suite all --baseline eval/baseline.json`：任何指标跌幅超过阈值（如问数 −3pp）则非零退出，CI/本地提交前跑。

### 12.4 压测

`app/eval/loadtest.py`：asyncio + httpx，50 并发 × 60s，记录 P50/P95、错误率、降级触发率、单任务 token 成本。

### 12.5 目标数字（跑出来再写，禁止先编）

| 指标 | 目标 |
|---|---|
| 问数四项匹配率 | ≥ 0.90 |
| 归因 top-3 命中 | ≥ 0.80 |
| 策略卡 top-3 命中（gold root_cause 可接受集） | ≥ 0.80（首轮实测后校准） |
| 端到端任务成功率 | ≥ 0.75 |
| P95 延迟 | ≤ 8s |
| 单任务成本 | ≤ ¥0.30 |
| 50 并发错误率 | < 1% |

---

### 12.6 实测结果（2026-11-21，T7）

完整报告见 `docs/eval-report.md`（逐条结果、消融、差距分析都在里面），这里只留结论数字。

**四个套件（`python -m app.eval.runner`，`data/store.db`）**

| 套件 | 通过 | 关键指标 | 门禁 | 结论 |
|---|---|---|---|---|
| 问数（20） | 20/20 | accuracy 1.000 | 1.00 | PASS |
| 诊断（25） | 11/25 | top-1 0.440 / top-3 0.520 / precision 0.387 | 0.40 / 0.48 / 0.35 | PASS |
| 动作（15） | 15/15 | accuracy 1.000（含 5 条护栏拒绝） | 1.00 | PASS |
| 素材（10） | 10/10 | field accuracy 1.000（mock）/ 0.883（vlm） | 0.90 | PASS |

**门禁阈值是实测值往下留 2~4 个点**，不是 spec §3.8 的目标值。目标（top-3 ≥0.80）暂时没达到，
差距与原因写在报告 §6，不靠改阈值掩盖。

**评测期间修掉的三处引擎问题**（同一批 25 条、同一份真值，top-1 0.360→0.440、top-3 0.400→0.520、
precision 0.287→0.387）：

1. 稀有事件（退款/差评）在小分母上噪声压过真实下滑 → 事件数 ≥3、`RARE_Z_MIN=2.2`、相对变化封顶 100%；
2. 退款原因与根因无关，R4/R10 不可分 → 数据 v2：R4 窗口内 80% 记 `SIZE_SMALL`、R10 窗口内 80% 记 `LOGISTICS`；
3. R6 只认 `neg_review_rate`，误删「评分已掉、差评率未起」的真 R6 → 改为双证据（`dsr` 为准，`neg_review_rate` 为辅），
   replan 不再把 R6 当必需辅证。

**消融**：召回 `fts-only 0.857` → `hybrid 1.000`（向量补上 2 字词漏召回）；
重排 off/rule/cross 三档并列 1.000/0.857（7 条 query 太容易，只能证明「不劣化」）；
第二轮复审 `replan=on` 精度 +7.5 点、召回 −5 点、每条 +0.4s。

## 13. 接口契约

实现以本节为准（下表为**已实现**的端点，W3 的动作闭环另见 §9）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | SPA（`web/dist/index.html`，存在才挂载） |
| GET | `/api/health` | 健康检查：db 行数/最后日期、`llm{configured,active,model_planner,planner}`、`catalog_version`、`metrics_count` |
| GET | `/api/metrics/catalog` | 指标目录（23 个指标 + 维度组 + 限额） |
| POST | `/api/query` | 直接跑 QuerySpec（调试与评测用） |
| GET | `/api/overview` | 体检台聚合：`days`、`as_of`；KPI/健康分/卡片/30 天走势/款式分布 |
| GET | `/api/diagnose` | 诊断：`as_of`、`window_days`（1..30）、`max_cards`（1..10）、`critic`（`rules`\|`llm`） |
| POST | `/api/diagnose` | 同上，body 形式（`DiagnoseRequest`） |
| POST | `/api/chat` | 问诊：`{message, as_of, window_days}` → SSE（plan/tool_call/tool_result/diagnosis/evidence/playbook/token/done/error） |
| POST | `/api/playbook/search` | 策略卡检索：`query` 或 `root_causes` 二选一，`risk_max`、`limit`、`mode` |
| GET | `/api/llm/probe` | 网关探活：真发一次「只回四个字」，返回模型/延迟/tokens |
| GET | `/api/images/{sku_id}` | 合成素材图（主图/详情页），给前端与 `vision.inspect` 用（W2 素材诊断） |
| POST | `/api/vision/inspect` | `{sku_id, role}` → 结构化素材属性（W2 素材诊断） |
| POST | `/api/actions/dry-run` | `{action}` → 影响预估（W3） |
| POST | `/api/actions/{id}/approve` | `{approver}`（W3） |
| POST | `/api/actions/{id}/execute` | 执行（幂等，W3） |
| POST | `/api/actions/{id}/rollback` | 反向动作（W3） |
| GET | `/api/audit` | 审计列表（W3） |
| GET | `/api/trace/{trace_id}` | trace JSON（W3；返回 `{trace_id, actions[], events[], count}`，`count` 是事件数，事件顺序不保证，前端按 `created_at` 排） |

统一错误体：`{code, message, detail, trace_id}`；当前实现的错误码：`invalid_spec` / `invalid_query` / `invalid_window` / `invalid_date` / `invalid_risk` / `invalid_mode` / `invalid_critic` / `index_missing`。

**`invalid_spec` 对未知字段是硬错误**：`QuerySpec` 及其嵌套模型（`time` / `filters[]` / `order_by[]`）全部 `extra="forbid"`，传 `dims` 这类错名字会得到 400 并提示「未知字段「dims」；只接受 metrics / dimensions / time / filters / order_by / limit」。这条是验收时修的：以前未知字段被静默忽略，查询退化成全量汇总，看起来成功但口径全错。

---

## 14. 目录结构

```
Ecommerce/
├─ project/                   代码与产物根（命令都在这里跑）
│  ├─ app/
│  │  ├─ main.py              FastAPI 入口（CORS + 静态托管 + 路由装配）
│  │  ├─ api/                 routes.py（health / metrics.catalog / query / chat / actions / audit / trace）
│  │  ├─ core/                config.py, llm.py, errors.py, trace.py
│  │  ├─ metrics/             catalog.yaml, views.py, spec.py, compiler.py   ← 指标语义层
│  │  ├─ insights/            overview.py（体检台概览：KPI / 健康分 / 异常卡）
│  │  ├─ retrieval/           [W2] fts.py, embed.py, hybrid.py, rerank.py, index.py
│  │  ├─ data/                schema.sql, generator.py, seed.py, index_vectors.py
│  │  ├─ datasource/          [生产接入，见 §21] base.py, csv_import.py, taobao.py, youzan.py, registry.py, sync.py
│  │  ├─ diagnose/            anomaly.py, decompose.py, contribution.py, funnel.py, evidence.py, hypothesis.py, critic.py
│  │  ├─ playbook/            loader.py, search.py, cards/*.yaml（27 张）
│  │  ├─ actions/             model.py, dryrun.py, approval.py, executor.py, idempotency.py, audit.py
│  │  ├─ orchestrator/        state_machine.py, planner.py, executor.py, reporter.py, tools/*.py
│  │  └─ eval/                tasks/*.jsonl, run.py, scoring.py, loadtest.py, baseline.json
│  ├─ web/                    Vite + React + TS（vite.config.ts 代理 /api → 8000）
│  ├─ data/                   store.db、images/（生成物，gitignore）
│  ├─ tests/                  单元 + 集成（pytest，httpx.ASGITransport）
│  ├─ tools/                  compat_check.py, sanity_check.py, metrics_smoke.py
│  ├─ docs/                   本 spec、tech-compat.md、multimodal-jd-scan.md
│  ├─ docker/                 [W3] Dockerfile、compose.yaml
│  └─ pytest.ini
├─ reference/                 参考文档（git 忽略）
└─ README.md                  GitHub 门面
```

---

## 15. 里程碑与验收

### W1（11-19 ~ 11-25）：能看见

- 数据生成器 + 11 张表 + seed 脚本（可复现）
- 23 个指标 YAML + QuerySpec 编译器 + 安全网关 + `/api/query`
- 异常检测 + 恒等式分解 + 维度下钻 + 诊断卡 JSON
- 前端骨架：对话页 + 诊断卡（SSE mock）
- 验收：3 分钟能演示「问一句 → 出诊断卡 → 点开证据」

### W2（11-26 ~ 12-03，含多模态顺延 1.5 天）：能归因

- 漏斗 + 证据链 + 根因假设 + Critic
- 策略卡 **27 张**（从 30 张砍 3 张非核心卡，为素材诊断腾 1.5 天）+ 影响预估
- **混合检索 + 三级重排**：FTS5 + bge-m3 + RRF、规则分 / cross-encoder / LLM listwise（§20），含降级与懒加载
- **素材诊断**：`vision.inspect` 工具 + `image_feature` + 素材证据 + 竞品视觉差异表（详见 §19）
- 编排状态机 + 工具注册 + LLM 网关 + 降级路径
- 20 条问数 + 25 条诊断 + 10 条素材评测跑通
- 验收：归因 top-3 ≥ 0.80，问数四项匹配 ≥ 0.85，素材属性抽取准确率 ≥ 0.85，策略卡 top-3 命中 ≥ 0.80，检索链路 P95 ≤ 2s（`cross` 档）

### W3（12-04 ~ 12-10）：能执行

- Action schema + dry-run + 分级审批 + 幂等 + mock 执行 + 审计 + 回滚 + 复盘
- 审批台前端 + trace 查看器 + 素材对比视图
- 70 条评测 + 消融 + 回归门禁 + 50 并发压测
- Docker compose、README、demo 脚本、文章草稿
- 验收：端到端 ≥ 0.75，P95 ≤ 8s，50 并发错误率 < 1%，一键起

---

## 16. 依赖

### 16.1 Python（venv `E:\llm\third\test\k12\.venv`，已具备，实测 11/11 通过）

fastapi 0.141.1、starlette 1.6.0、uvicorn 0.52.1、sqlalchemy 2.0.51、aiosqlite 0.22.1、pydantic 2.13.4、pydantic-settings 2.15.0、python-dotenv 1.2.2、openai 2.53.0、httpx 0.28.1、tenacity 9.1.4、pyyaml 6.0.3、pytest 9.1.1、orjson 3.11.9、rich、typer

**不依赖**：duckdb、streamlit、pandas（生成器用 stdlib + `executemany`，避开 pandas 3.0 的 API 变动）、faker（用固定词表）、sqlglot（可选增强）。

**可选（仅本地检索/重排需要，已装在 venv）**：`requirements-embed.txt` = torch、sentence-transformers、transformers、numpy（向量矩阵）。**不写进主 `requirements.txt`**——否则别人 clone 下来光装依赖就要 2GB+；主流程只依赖 FTS5，缺这些包时自动降级。

### 16.2 前端（钉版本，需手动安装）

```json
"dependencies":    { "react": "18.3.1", "react-dom": "18.3.1" },
"devDependencies": { "vite": "7.3.8", "@vitejs/plugin-react": "5.2.0",
                     "typescript": "5.9.3",
                     "@types/react": "18.3.31", "@types/react-dom": "18.3.7" }
```

`npm install --dry-run` 实测：71 个包、无 peer 冲突。**`@types/react` 必须钉 18**，否则 npm 会装 19.x 与 react 18 类型打架。装完先跑 `npx tsc --noEmit && npm run build` 验证工具链。

### 16.3 环境变量 `.env`

```
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-...
LLM_MODEL_PLANNER=deepseek-flash
LLM_MODEL_FAST=deepseek-flash
LLM_MODEL_VISION=deepseek-flash
LLM_MODE=auto                   # auto | real | mock；auto 有 key 走真、无 key 自动规则版
LLM_THINKING=off                # off 关思考链（实测规划 8.7s → 0.9s）；网关不认该参数时改 on
LLM_TIMEOUT_MS=12000            # 单次调用超时；端到端预算 8s，别给太长
LLM_RETRIES=1                   # 只对超时/连接/429/5xx 重试，认证错误不重试

# 检索与重排（D10，见 §20）
EMBED_MODE=local                # off | local | api；off 时只用 FTS5
EMBED_MODEL_PATH=E:\llm\hfmodel\models--BAAI--bge-m3\snapshots\5617a9f61b028005a4858fdac845db406aefb181
EMBED_DIM=1024
RERANK_MODE=rule                # rule | cross | llm；cross 需本地模型，llm 需 LLM key
RERANK_MODEL_PATH=E:\llm\hfmodel\bge-reranker-v2-m3
RERANK_TOP_N=6                  # 只对 top-N 候选做 cross-encoder
RERANK_TIMEOUT_MS=3500          # 超时降级回规则排序

# 数据接入（§21）
DATA_SOURCE=synthetic           # synthetic | platform；platform 异常自动回落 synthetic
STORE_ID=store_001              # 多租户隔离键，所有查询强制带上
SYNC_LOOKBACK_DAYS=30           # T+1 增量回溯窗口（补数用）
```

`EMBED_MODEL_PATH` 与 `RERANK_MODEL_PATH` 同时兼容两种布局：普通目录（含 `config.json` + `model.safetensors`）和 HF cache（`models--<org>--<name>/snapshots/<hash>/`，取最新快照）。路径不存在时**不报错**，自动降级并在 `meta.retrieval` 里标注 `degraded: true` 与原因。

模型名以 `GET /models` 返回为准。DeepSeek 的多模态模型是 `deepseek-flash`（旧名 `deepseek-v4-flash-vision-exp` 仍可调，已下线由 Flash 承接）；图片走 OpenAI 兼容 `content` 块数组，`image_url` 支持 base64 data URL / 公网 URL / Files API `file_id`，`detail=low` 会缩到 512×512 最省 token，单图 ≤1024 tokens，价格与 Flash 相同。

**思考链必须关**：`deepseek-flash` 默认先输出 `reasoning_content`，而 `max_tokens` 把思考也算进去——实测同一条规划问题，开思考 8718ms / 644 tokens（其中 488 是 reasoning，正文 0 字时直接解析失败），关掉后 2162ms / 279 tokens 且 JSON 更规矩（不再塞 `metrics`、`compare` 这类白名单外的参数）。所以 `LLM_THINKING=off` 时网关在请求里带 `extra_body={"thinking":{"type":"disabled"}}`；换成不认这个参数的网关（通义、硅基等）时，网关会捕获「unknown field / thinking」类报错，去掉 `extra_body` 自动重试一次并打警告，同时把 `LLM_THINKING=on` 写进文档。

### 16.4 环境约束（实测，写进代码约定）

1. **禁 `tempfile.mkdtemp` / `%TEMP%`**：本机沙箱下其子目录 ACL 异常（`WinError 5`）。临时产物统一落 `data/`、`.tmp/`，启动时设 `TMPDIR`。
2. **测试用 `httpx.ASGITransport`**，不用 `TestClient`（starlette 1.6 已弃用，会告警）。
3. **SQLite 连接**：`PRAGMA journal_mode=WAL` + `busy_timeout=5000` + 读连接 `mode=ro`；慢查询用 `set_progress_handler` 中断（已验证）。
4. 详见 `docs/tech-compat.md`，自检脚本 `tools/compat_check.py`。
5. **素材图不外发**：`vision.inspect` 只把合成图或用户显式提供的图发给 VLM；不落盘原始图片到第三方存储，不缓存原图，只缓存结构化结果。

---

## 17. 风险与止损

| 风险 | 影响 | 止损 |
|---|---|---|
| LLM key 不可用 | Agent 层跑不了 | Gateway 抽象 + 模板降级路径，评测用缓存回放 |
| 前端延期 | demo 缺一块 | API-first，退回 Jinja2 服务端渲染 |
| 归因不准 | 评测数字难看 | 先规则后 LLM；ground truth 对齐后再调 |
| 20 万行查询慢 | P95 超标 | 预聚合表 `agg_daily`（W1 就建） |
| 3 周不够 | 交付不完整 | 砍动作类型到 3 种，保诊断与审批主链路 |
| Docker daemon 未启动（本机实测） | `docker compose up` 验不了 | W3 前请你启动 Docker Desktop；在此之前用本地 venv + `npm run dev` 演示 |
| npm 脚本策略拦截 esbuild postinstall | 前端构建失败 | `npm install-scripts approve esbuild` |

---

## 18. 交付物

- GitHub repo（`docker compose up` 一键起，含 seed + eval）
- 5 分钟 demo 脚本（分镜 + 旁白）+ 录屏
- 评测报告（70 条 + 消融 + 压测 + 成本）
- 技术文章《电商经营 Agent 的指标语义层、归因与评测》
- 简历三行 + 面试 8 问答案（数字回填）

---

## 19. 素材诊断（D9 展开）

只做「用视觉做诊断证据」这一层，不做生图、不训模型。JD 依据见 `docs/multimodal-jd-scan.md`。

### 19.1 工具与输出

```
vision.inspect(sku_id, role="main" | "detail" | "scene") -> MaterialFeatures
```

```json
{
  "sku_id": "SKU0123", "role": "main", "source": "vlm",
  "text_blocks": ["雪纺连衣裙", "显瘦遮肉", "3 色可选"],
  "text_coverage": 0.38, "model_present": true, "scene": "室内棚拍",
  "background": "纯色", "color_palette": ["#F2E9DD", "#8B6F5C"],
  "size_chart_present": false, "watermark": false, "collage": false,
  "quality_flags": ["无尺码表", "文字占比偏高"], "confidence": 0.82
}
```

落 `image_feature(sku_id, role, features_json, source)`；**存结构化 JSON，不存图、不存向量**。图片本身放 `data/images/{sku_id}_{role}.png`，不发给第三方存储。

### 19.2 从特征到证据（关键在映射，不在识别）

| 视觉特征 | 对比基线 | 产出证据 | 关联根因 | 动作 |
|---|---|---|---|---|
| 无尺码表 | 同类目高转化 SKU 中位数（如有尺码表占 78%） | ctr/cvr 差 + 退货率 | R4 尺码信息缺失 | `swap_creative` |
| 文字占比 > 0.35 | 高转化 SKU 通常 < 0.20 | ctr 下降贡献 | R7 素材衰减 | `swap_creative` |
| 主图纯白底无场景 | 高转化多为场景图 | ctr 差距 | R7 素材衰减 | `swap_creative` |
| 与竞品主图属性同质 | 竞品差异表 | 差异化不足 | R8 竞争加剧 | `swap_creative` / `adjust_price` |

每条证据必须带 `(指标, 维度值, 时间窗, 变化量, 贡献占比)`，和数值证据同一套 schema —— 这样 Critic 能用同一套规则审，前端能用同一个 chip 渲染。

### 19.3 竞品视觉对比

`competitor.compare` 追加一列 `main_image_attrs`，返回 `{competitor, price, rating_cnt, attrs:{...}, diff:[...]}`，前端渲染成差异表。这是阿里国际那份 JD 里的原话（"自动生成多维度竞品对比表"），成本最低、命中关键词最直接的一处。

### 19.4 合成素材（评测用，不冒充真实商品图）

生成器程序化画主图，6 个维度组合：背景（纯色/场景/渐变）、模特（有/无剪影）、文字块（0/1/2 条 + 占比）、尺码表（有/无）、水印（有/无）、拼接（是/否）。转 PNG 存 `data/images/`，**属性标签即 ground truth**，同时写 `image_feature(source='mock')` 供无 key 时回放。

**第一版踩的坑（必须记住）**：生成器是「先定属性、再随手画」，`text_coverage=0.38` 的图实际文字只占 10%，模特是个人台色块——视觉模型看到的东西和标签对不上，vision suite 实测 0/10。这不是模型不行，是**图和标签不一致**。

修法见 `project/tools/rerender_images.py`（反过来做：读属性 → 按属性画 → 量出真实占比 → 回写 `features_json`，并用 `derive_flags` 重算 `quality_flags`，保证标签与阈值不打架）：

| 属性 | 画法 |
|---|---|
| `model_present` | 画完整人物（头发、五官、颈、肩臂、手、腿、鞋、地面阴影），只画「圆+梯形」模型不认 |
| `text_coverage` | 左侧竖栏文字块按目标面积迭代缩放（±6%），画完用块面积回算真实占比 |
| `scene` | 每个场景放地标：度假=太阳+海平线+沙滩、街拍=楼群+人行道、室内=墙面+地板、居家=沙发+绿植、纯白=纯白 |
| `collage` | 2×2 分缝拼贴，四格不同底色 + 白色分缝 |
| `watermark` | RGBA 图层叠半透明 SAMPLE（RGB 直接 `alpha_composite` 会报 wrong mode） |
| `size_chart_present` | 画「尺码表」表格 + S/M/L/XL 胸围行 |

### 19.5 评测 `vision` suite（10 条，单列计分）

分层抽样 10 条（按问题码取样，seed 固定），6 个字段判分：`size_chart_present` / `watermark` / `collage` / `model_present` 全等、`text_coverage` 允许 ±0.10、`scene` 归一到「纯白底/室内/户外/度假」后比对。

**两个口径并列报**：整条准确率（6 字段全对）与**字段准确率**（`fields_ok / total_fields`）。门禁用字段准确率 ≥ 0.85——素材抽取是多标签任务，整条全对会把「5 对 1 错」也判失败，失之过严。

实测（`python -m tools.vision_eval --mode vlm`，2026-11-19）：

| 模式 | 整条 | 字段 | 耗时 | 说明 |
|---|---|---|---|---|
| mock（回放真值） | 10/10 | 60/60（100%） | 26ms | 离线基线，验证评测脚本本身 |
| vlm（deepseek-flash, detail=low） | 3/10 | **53/60（88.3%）** | 12.8s（≈1.3s/图） | 残余错误集中在 `text_coverage` 边缘（0.48 读成 0.35）、度假/街拍场景混淆、拼贴图里的模特漏判 |

结论：字段级 88.3% 过门禁，但**不能说「视觉能力很强」**——真实商品图比合成图信息量大得多，这个数字只能证明链路与判据是通的。

### 19.6 降级与成本

- 无 key：回放 `image_feature(source='mock')`，UI 标注「离线样例，非实时推理」，评测单独统计不计入主分；
- 缓存：`key = sha256(image_sha256 + prompt_version)`，同一张图不重复付费；
- 成本：单任务最多 10 张图 × ≤1024 tokens ≈ 1 万 token，几分钱量级；`detail=low` 再省。

### 19.7 风险

| 风险 | 止损 |
|---|---|
| 合成主图「太假」，面试被质疑 | README 与 UI 明确标注「合成素材」；预留 `data/images/real/` 目录，你有真实商品图可直接替换重跑 |
| VLM 输出不稳定（字段缺失/幻觉） | 强制 JSON schema + 字段级校验，缺字段记 `null` 不算过；temperature 0；两次不一致取交集 |
| 视觉成本超预算 | 每任务限 10 张图 + `detail=low` + 缓存命中 |

---

## 20. 检索与重排（D10 展开）

语料只有 37 条（10 条平台规则 + 27 张策略卡），目标不是「做一个 RAG 知识库」，而是**让策略卡的选择可解释、可评测**。所以这里没有向量库，只有两路召回和三级重排。

### 20.1 为什么不引向量库

| 方案 | 判定 |
|---|---|
| chromadb / Milvus | ✗ 依赖重；chromadb 默认 embedding 函数首次调用会联网下载 ONNX 模型，离线即挂；检索结果无法直接进证据链 |
| 纯 FTS5 | ✓ 零依赖、毫秒级、可解释，但「图不好看」搜不到「素材同质化」 |
| **FTS5 + 本地 bge-m3 + RRF** | ✓ 本机有模型；37×1024×4 ≈ 148KB，存 SQLite BLOB；`numpy` 余弦全量扫 <1ms |

### 20.2 索引与存储

```sql
CREATE TABLE embedding (
  owner_table TEXT NOT NULL,      -- 'policy_docs' | 'playbook_cards'
  owner_id    TEXT NOT NULL,
  model       TEXT NOT NULL,      -- 'bge-m3'
  dim         INTEGER NOT NULL,   -- 1024
  vec         BLOB NOT NULL,      -- float32 小端连续内存，np.frombuffer 还原
  text_hash   TEXT NOT NULL,      -- 原文 hash，变了才重建
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner_table, owner_id, model)
);

CREATE VIRTUAL TABLE search_fts USING fts5(
  owner_table UNINDEXED, owner_id UNINDEXED, body,
  tokenize = 'trigram'            -- 中文按 3-gram 切，不需要额外分词器
);
```

- 建索引走 `python -m app.data.index --target policy,playbook`，37 条实测约 5s（7 条/秒），**在 seed 阶段做**，不进请求路径；
- `text_hash` 让重复执行变成 no-op；换模型只追加行，不覆盖。
- trigram 的代价：查询串 <3 字符时退化为 LIKE 前缀匹配；够用，但要在 `meta.retrieval.fts_note` 里标出来。

### 20.3 混合召回

1. **硬过滤**（先砍候选，再谈相关性）：`root_cause` 精确匹配、`metric` 覆盖、`risk_level` 与审批人匹配、`scope`（全店/SKU）匹配；
2. **FTS5** 取 top-12，BM25 排序；
3. **向量**：查询 embed（实测 0.17s）→ 余弦 top-12；
4. **RRF 融合**：`score = Σ 1/(k + rank_i)`，`k=60`。不调权重——两路分数量纲不同，RRF 只看名次，稳且不需要调参；
5. 输出 top-12 候选，每项带 `fts_rank` / `vec_rank` / `rrf_score`，全量进 trace。

### 20.4 三级重排

**第一级：规则分（默认，必做）**

```python
score  = 3.0 * (card.root_cause == evidence.root_cause)
score += 2.0 * (card.metric in evidence.metrics)
score += 1.5 * card.trigger_satisfied(evidence)
score += 0.5 * success_rate(card.id)          # 来自 action_log 的复盘结果
score -= 1.0 * (card.risk_level == 2 and not approver)
score += 0.3 * freshness(card.updated_at)
```

耗时 <1ms，`score_breakdown` 原样进 trace——面试问「为什么选这张卡」时，能指着数字回答。

**第二级：cross-encoder（`RERANK_MODE=cross`）**

`bge-reranker-v2-m3` 对 top-6 候选逐对打分（`query` + 卡片全文），sigmoid 输出 0–1。它和 bge-m3 的本质差别：bi-encoder 两段文本各自编码、看不到彼此 token；cross-encoder 在注意力层直接交互，对「否定 / 条件 / 数字」敏感得多，代价是不能预计算。

- 用途是**过滤器**而非排序器：`score < 0.2` 直接判「不相关」；通过的候选再用规则分排先后（两者量纲不同，不混合加权）；
- 全部低于 0.2 → 输出「策略库无匹配卡，建议人工介入」，不硬凑；
- 超时 `RERANK_TIMEOUT_MS=3500` 或模型缺失 → 降级回规则，`meta.rerank = "rule(fallback)"`。

**第三级：LLM listwise（`RERANK_MODE=llm`，需 key）**

只让模型**排序**，不让它生成内容，避免编造：

```
你是电商运营专家。下面是店铺诊断证据和 5 张候选策略卡。
证据：指标=退款率, 维度=甜美风连衣裙, 变化=+73%, 根因=R4 尺码信息缺失
候选：[1] 补全尺码表并置顶(risk=0) [2] 调整价格带(risk=1) ...
只输出按相关性从高到低的编号，用逗号分隔，不要解释。
```

- 只喂 top-5（prompt 约 800 token，单次约 ¥0.002），延迟 2–3s；
- 缓存键 `sha256(query_norm + candidate_ids + model)`，评测 70 条能省掉大量调用；
- 返回校验：编号必须都在候选集内、去重、缺失按原序补齐（模型返回 `1,1,7` 是常态）；
- 失败一律降级回上一级，**重排不能成为单点故障**。

### 20.5 本机实测（CPU）

| 项 | bge-m3（召回） | bge-reranker-v2-m3（精排） |
|---|---|---|
| 加载 | 47.4s（含 import torch） | **18.8s** |
| 单条 / 单对 | 0.17s | 0.32s |
| 批量 | 7 条/秒 | — |
| 10 条候选 | — | **3.2s** |
| 常驻内存 | 1.9 GB | 1.9 GB |
| 输出 | 1024 维 | 0–1 概率 |

`max_length` 512 vs 256 实测 3.19s vs 3.15s——**没有区别**，卡片文本本来就短，瓶颈在模型宽度。所以别在这上面调参。

排序质量（真实 case：退款率 6.1%→11.6%、甜美风连衣裙、差评「偏小」、无尺码表）：

```
0.9855  C-R4 补全尺码表并置顶      ← 正确
0.0149  C-R10 催物流并补偿
0.0037  C-R2 更换主图素材
```

**66 倍分差**——这既支撑了 0.2 阈值，也说明 cross-encoder 的分数可以直接当「相关性判定」用。

### 20.6 冷启动与内存策略

两个模型都常驻 = 3.8GB，不能无脑加载：

| 模式 | 加载什么 | 适用 |
|---|---|---|
| `EMBED_MODE=off`（默认 fallback） | 都不加载 | CI、无 torch 环境、评测基线 |
| `EMBED_MODE=local, RERANK_MODE=rule`（推荐默认） | 只加载 bge-m3 | 日常开发 / demo |
| `EMBED_MODE=local, RERANK_MODE=cross` | 两个都加载，reranker **懒加载** | 需要精排时 |

- 懒加载：首次用到才加载，期间 SSE 推 `tool_call{name:"playbook.search", state:"warming"}`，前端显示「策略卡排序中」，不让用户干等 19 秒；
- 启动时后台线程预热 embed 模型，`/api/health` 暴露 `retrieval: {embed_ready, rerank_ready, mode}`；
- 进程内 `torch.set_num_threads(min(8, os.cpu_count() or 4))`，避免和 uvicorn 抢核。

### 20.7 延迟预算与评测

| 档位 | 检索耗时 | 说明 |
|---|---|---|
| `fts` | ~5ms | 纯关键词 |
| `fts+vec` | ~180ms | 含 query embed |
| `fts+vec+cross` | ~1.9s | top-6 精排 |
| `fts+vec+cross+llm` | ~3.5s | top-5 listwise |

四档都在 P95 ≤ 8s 的预算内，但要和 LLM 规划/生成错开统计，不能叠在同一个请求上还要求 8s。

评测口径：25 条诊断 case 的 gold root_cause 决定「可接受卡集合」，看 top-3 是否至少命中一张；同时记录「无匹配卡」的误判率（该给建议却说没有）。

### 20.8 风险

| 风险 | 止损 |
|---|---|
| torch 全家桶让 clone 变重 | 只放 `requirements-embed.txt`；主流程 FTS5 可独立跑通，README 注明「不装也能演示，只是排序更笨」 |
| CPU 上精排抖动（同机跑压测时 0.32s/对会退化） | 压测与 demo 用 `RERANK_MODE=rule`；cross 只在单请求场景开 |
| 模型路径写死在 `.env` 里，换机即失效 | 路径不存在时自动降级并 `degraded:true`，同时 `/api/health` 给出明确提示 |
| LLM 重排返回脏数据 | 编号校验 + 去重 + 补齐；失败降级规则排序，不抛错 |

---

## 21. 数据来源与生产接入

回答两个问题：**现在的数据是哪来的**，**正式使用时从哪取**。结论先说：现在全是合成的，且必须标清楚；生产接入不重写语义层，只换 adapter。

### 21.1 当前：合成数据（只服务评测与演示）

生成器 `app/data/generator.py` 用 `--seed 42` 复现，纯标准库 + Pillow，不依赖 Faker（离线可跑）：

| 项 | 取值 |
|---|---|
| 规模 | 300 SKU / 180 天（2026-05-23 ~ 2026-11-18）/ 196,213 单 / 82 MB SQLite |
| 维表 | `dim_date`、`dim_sku`、`dim_channel`、`dim_segment` |
| 事实表 | `fact_traffic_daily`、`fact_orders`、`fact_refunds`、`fact_ads`、`fact_reviews`、`fact_competitor_price`、`inventory_snapshot` |
| 业务规则 | 连衣裙季节性（7 月 ×1.35、11 月 ×1.30）、周末因子、618 / 99 划算节 / 双 11 促销、帕累托热度（`rng.paretovariate(1.35)`）、5 渠道 × 5 细分 |
| 注入根因 | 10 类（R1–R10）写 `anomaly_gt`，含窗口、SKU 集、幅度、根因码 |
| 素材 | Pillow 画 300 张 800×800 主图，6 维属性即真值，落 `image_feature(source='mock')` |

**为什么必须合成**：真店数据没有 ground truth，「归因 top-3 命中 0.80」这句话就无从验证。`anomaly_gt` 是本项目唯一能算准确率的东西，也是它区别于「又一个 RAG 问答」的地方。

**边界（必须对面试官/用户讲清楚）**：

- 不能作为经营依据，只能证明流程与评测方法成立；
- 竞品价、评价文本、退款原因是简化模型，真实分布会更脏；
- 合成数据「太干净」——真实数据有空值、重复单、跨日退款、SKU 改名，§21.4 的质量校验就是为此准备。

### 21.2 生产数据的四条路

| 优先级 | 来源 | 拿到什么 | 门槛 | 落地成本 |
|---|---|---|---|---|
| 1 | **后台导出**（生意参谋 / 京东商智 / 抖音罗盘 / 千川 / 万相台）CSV、Excel | 流量、转化、成交、投放汇总 | 无开发，人工导出，T+1，粒度常到 SKU 日 | 0.5 天（做导入器） |
| 2 | **平台开放平台 API**（淘宝开放平台 `taobao.trade.*`、京东 JOS、拼多多 `pdd.order.list.get`、抖店 OpenAPI、Amazon SP-API） | 订单、商品、退款、评价 | 企业资质 + 应用审核 + 店铺 OAuth + QPS 限流 | 3~5 天 + 审核周期 |
| 3 | **SaaS / ERP 开放接口**（有赞云、微盟、聚水潭、旺店通、店小秘） | 订单、库存、商品、客服会话 | 服务商资质或商家授权，接口最规整 | 2~3 天 |
| 4 | **自建埋点**（GA4、神策、自建 event） | 曝光、点击、详情页 UV、加购 | 只有自有站/独立站才有 | 视站点而定 |

平台不直接给的：**竞品价格**（只能第三方数据服务或自建爬虫，合规风险最高）、**站外流量**、**买家画像**。

### 21.3 指标 → 真实来源映射

语义层只认 `source: traffic | orders | ads | reviews | inventory`，所以换平台不改 `catalog.yaml`：

| 指标组 | 指标 | 真实来源 |
|---|---|---|
| traffic | impressions / clicks / ctr / detail_uv / add_cart_uv / add_cart_rate / cvr / uv / pv | 生意参谋「流量」、商智、罗盘；或自建埋点原始事件 |
| orders | gmv / paid_orders / aov / unit_price / gross_margin / refund_rate / return_rate | 订单表（开放平台或 ERP） |
| ads | ad_spend / roi / cpc | 万相台、京准通、千川的消耗与成交报表 |
| reviews | neg_review_rate / dsr | 评价 API（如 `taobao.traderate.*`） |
| inventory | stockout_rate | ERP 库存快照 |
| 竞品 | competitor_price | 第三方数据服务（慢慢买 / 店查查）或公开价格页 |

两个口径坑，接真数据时必然遇到，现在就要写死：

1. **退款归属**：退款发生在退款日、成交发生在支付日，退款率必须 JOIN 订单按**支付日**归属（本项目已按此实现）；
2. **曝光口径**：平台「曝光」与埋点「曝光」不是一回事，混用会让 CTR 出现倍数级偏差，必须在 `catalog.yaml` 的 `description` 里注明来源。

### 21.4 adapter 层设计

```
app/datasource/
  base.py        SourceAdapter 协议
  csv_import.py  生意参谋 / 商智 / 罗盘导出模板
  taobao.py      淘宝开放平台
  youzan.py      有赞云
  jushuitan.py   聚水潭
  registry.py    按 DATA_SOURCE 选源
  sync.py        增量同步 + 幂等落库 + 质量校验
```

```python
class SourceAdapter(Protocol):
    name: str
    grain: str                     # 'sku_day' | 'order'

    def auth(self) -> None: ...    # OAuth / token 刷新，token 存 .secrets（gitignore）
    def fetch(self, store_id: str, start: date, end: date) -> Iterator[dict]: ...
    def normalize(self, row: dict) -> dict: ...   # → dim_* / fact_* 行
```

五条硬规则：

- **幂等**：`INSERT ... ON CONFLICT(dt, sku_id, channel_id) DO UPDATE`，主键即幂等键，重跑不重复计；
- **增量**：`sync_state(table_name, last_dt, watermark, updated_at)` 记水位，`SYNC_LOOKBACK_DAYS=30` 回溯补数；
- **质量校验**（同步后必跑，不过就拒写并告警）：行数环比 > ±50%、`pay_amount` 非空率 < 99%、`gmv = 销量 × 均价` 偏差 > 0.1%、日期出现未来值；
- **血缘**：每行带 `source` / `fetched_at` / `batch_id`，出问题能定位到批次；
- **可回落**：`DATA_SOURCE=platform` 拉取失败时自动回落 synthetic 并在 `/api/health` 标 `degraded`，演示不空屏。

### 21.5 多租户与合规

- **多租户**：所有查询强制 `store_id`；当前单店，`views.py` 预留 `WHERE store_id = ?`，schema 的 `dim_*` / `fact_*` 加 `store_id` 列（默认 `store_001`）。
- **PII 不入库**：买家手机号、地址、收件人在 ETL 阶段丢弃或 HMAC 哈希，只留 `buyer_id_hash`；日志里的用户输入做 PII 正则打码。
- **平台协议**：数据不得转售或二次分发；爬虫遵守 `robots.txt` 与《反不正当竞争法》，只取公开价格页，不绕登录、不破解接口。
- **留存**：原始响应只留 7 天用于排错，落库只留结构化字段。

### 21.6 落地顺序（成本从低到高）

1. **CSV 导入**（0.5 天）：拿一份真店导出报表跑通，验证语义层口径——这是性价比最高的一步；
2. **有赞云或聚水潭**（2~3 天）：OAuth + 订单/商品增量，接口规整、审核门槛低；
3. **淘宝 / 京东开放平台**（3~5 天 + 审核）：最主流也最难，需企业资质；
4. **竞品与评价**：有授权才接，没有就用合成/人工，别硬爬。

每一步的验收标准都一样：**`catalog.yaml` 一行不改，五个视图 SQL 不改，只切 adapter**。

### 21.7 风险

| 风险 | 止损 |
|---|---|
| 接口审核不通过 / 无企业资质 | 先走 CSV 导入，demo 保持合成数据，讲述时说明 adapter 已就绪 |
| 真数据口径与合成数据不一致（退款日 vs 支付日、曝光口径） | §21.3 两条口径先写死，接第一个源时用 `tools/sanity_check.py` 对一遍 |
| QPS 限流导致 T+1 跑不完 | 分页 + 断点续传 + 按 `sku_id` 分片；限流时先落原始分片再离线清洗 |
| 合规踩线（爬竞品、存 PII） | 只取公开页、PII 哈希、原始响应 7 天留存；对外文档写明数据来源 |
| 多租户漏加 `store_id` | 编译层强制注入 `store_id` 谓词，单测断言「不带 store_id 的 SQL 生成直接抛错」 |

---

## 22. Critic：反方审查（D11 展开）

诊断引擎只会「往一个方向讲」：检测到 `ctr` 下跌就想到主图，检测到 `paid_orders` 下跌就想到断货。Critic 的任务是**当场反驳自己**，并把反驳折算成置信度——宁可少说一张卡，也不要把「大概率不是这个原因」的卡排在第一屏。

### 22.1 放在哪一步

```
detect（双窗口异常） → 方向过滤 → 分组 → hypothesis.rank（根因假设）
   → contribution（算钱） → 拼卡 → **critique（反方审查）** → apply_verdicts（改分/摘卡） → 排序输出
```

`critique` 是纯函数：输入卡片 dict 列表 + `promo_skew` + `market_delta_pct`，不查库、不联网，因此可以在测试里穷举反例。

### 22.2 七项检查

| 检查 | 触发条件 | 系数 | 在讲什么 |
|---|---|---|---|
| `direction` | 主指标方向与根因预期相反（如 R2 却 CTR 上涨） | 0.45 | 最硬的否决：连方向都不对 |
| `upstream` | R2 但曝光跌幅 ≥ 点击率跌幅；R8 但只有点击率跌、没有曝光证据；R7 缺促销偏斜；缺 `REQUIRED_SUPPORT` 辅证；辅证方向相反；R1 缺竞品价或价差 < 3% | 0.70 / 0.75 / 0.80 / 0.85 | 「有没有更上游的解释」 |
| `market` | 单品跌幅与大盘 GMV 同量级（≤1.5 倍），且指标是 `gmv`/`paid_orders` | 0.80 / 0.85 | 是不是只是随大盘 |
| `sample` | 最强证据 `|z| < 2.5`；或只有一条证据 | 0.80 / 0.90 | 会不会只是正常波动 |
| `promo` | 促销日占比变化 ≥ 0.30 且主指标是量价类，且根因不在 `PROMO_EXEMPT`（R3/R7） | 0.65 | 窗口量价不可比 |
| `impact` | 算不出成交额影响；影响 < ¥500；占整体变化 > 100% | 0.90 / 0.80 / 0.85 | 值不值得排前面 |
| `duplicate` | 同根因已有一张卡（按影响金额降序，被驳回的卡不占名额） | 0.85 | 同一个根因不要讲两遍 |

`EXPECTED_DIRECTION` 是十类根因与指标的期望方向表（如 R5 期望 `roi` 下降、`ad_spend` 上升），`REQUIRED_SUPPORT` 是「光看主症状不够，必须有这条辅证」的表（R3→`stockout_rate`、R4→`return_rate`、R5→`roi`、R6→`neg_review_rate`、R9→`unit_price`、R10→`refund_rate`）。

### 22.3 档位与写回

`confidence_after = clamp(confidence_before × Π factor, 0.05, 0.98)`：

| 区间 | verdict | 处理 |
|---|---|---|
| < 0.30 | `reject` | 从展示列表摘掉，写进 `meta.dropped`（`reason = "Critic 驳回：" + 首条 objection`） |
| 0.30 ~ 0.55 | `weaken` | 保留，但排在后面；`caveats` 追加「反方：…」 |
| ≥ 0.55 | `support` | 保留 |

`apply_verdicts` 幂等：同一张卡重复调用不会重复追加「反方：」条目；卡片自带的同类提示（促销口径、竞品价差、同根因、证据偏薄）也不重复。

### 22.4 LLM 反方审查（默认关闭）

`?critic=llm` 或 `CRITIC_LLM=true` 时，对前 3 张卡各发一次请求：只喂卡片摘要（code/标题/scope/主指标/置信度/最多 3 条证据/narrative 前 200 字/规则版反对意见），要求模型输出 `{objections[], confidence}`。三条约束：

1. **只降不升**：`confidence_after = min(规则版, max(模型值, 规则版 × 0.8))`——单张卡最多压 20%，否则模型一句「证据不足」就能把 0.83 压到 0.55，排序失去意义；
2. **模型说的 `verdict` 不参与裁决**，档位一律由置信度重算，避免「置信度 0.6 却 reject」的自相矛盾；
3. **预算**：`LLM_CRITIC_BUDGET_MS=4000`，`LLMError` 第一张失败即停，解析失败只跳过该卡；整体耗时超过 4s 就降级为规则版结论。

### 22.5 实测

6 个候选日（08-18 / 09-30 / 10-20 / 10-27 / 11-11 / 11-17），真值取 `anomaly_gt` 中**与诊断窗口有交集**的根因：

| | 卡数 | 命中 | 总体精度 | top-3 命中 |
|---|---|---|---|---|
| 无 Critic | 24 | 8 | 33% | 4/6 |
| 有 Critic（规则版） | 19 | 10 | **53%** | **4/6 = 67%** |

- 11-17（演示日）：5 张卡全部保留，`R3 0.83/0.71`（两张，第二张因同根因降权）、`R7 0.57/0.41`、`R4 0.38`；规则版耗时 **+几十毫秒**，接口整体 978ms。
- `?critic=llm`：LLM 复查 2568ms、3 张卡、整体 **5363ms**；模型意见举例——「支付订单数降 67.5% 远大于断货率升幅，断货不足以解释全部成交损失」「曝光下滑被归因于大促后透支，但卡片未给出任何大促期数据佐证」。
- 10-27：4 张误报卡被驳回 3 张（`R5 0.19 / R5 0.17 / R9 0.25`），只剩一张 `R6`——精度没提高但「噪音」显著下降；10-20 唯一的 `R6` 误报规则版驳不动，留给模型版与 W3 评测解决。

### 22.6 已知局限

| 局限 | 说明 |
|---|---|
| 规则版的根因映射仍以主指标为主 | 只看主症状的映射表天然会漏掉「多因叠加」；靠 `REQUIRED_SUPPORT` 与 `upstream` 部分补上 |
| 模型版不敢用 | 全量开着会把端到端推到 7s 以上，且倾向于「全部存疑」，所以只做演示开关 |
| 反例库靠人工 | 用 6 个日期 + 十类根因的注入真值做回归，还没做自动化的对抗样本生成 |
| `apply_verdicts` 对无匹配 verdict 的卡 fail-open | 有意为之：宁可少审一张，也不要在审查器出错时把诊断结果整片吞掉 |

## 23. 运行时设置（BYO-key，设置页）

### 23.1 为什么需要这一层

`.env` 是「出厂默认值」，适合开发者；但真实使用者会想用**自己的 key、自己的网关、自己的模型**，
不能要求他改文件再重启进程。设置页要解决的正是这件事：现场换 key、换供应商、换模型，保存后立刻生效。

### 23.2 优先级与生效链路

| 层 | 存在哪 | 谁写 | 何时生效 |
| --- | --- | --- | --- |
| 代码默认 | `app/core/config.py` | 开发者 | 进程启动 |
| `.env` | `project/.env`（已 gitignore） | 用户手改 | 进程启动 |
| **数据库覆盖** | `data/store.db` 的 `app_setting` 表 | 设置页 `PUT` | **下一次请求立刻** |

`get_settings()` 是 `lru_cache`，进程启动后 key 就固化了，所以新增 `app/core/runtime.py`
作为「生效配置」的唯一入口：读值时先看数据库覆盖，没有再回落到 `.env`。
保存设置时调 `runtime.refresh()`：清覆盖缓存 + `llm.reset_client()` 丢弃旧客户端。

### 23.3 表结构

```sql
CREATE TABLE app_setting (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

白名单 11 个字段（其余字段一律忽略，防越权写入）：
`llm_base_url`、`llm_api_key`、`llm_model_planner`、`llm_model_fast`、`llm_model_vision`、
`llm_mode`、`creative_provider`、`dashscope_api_key`、`creative_image_model`、
`creative_edit_model`、`creative_adapter`。

**空串语义 = 删除这条覆盖**（回到 `.env`），不是「把值清成空」——否则用户清空输入框
反而会把 `.env` 里能用的 key 顶掉。

### 23.4 接口

| 方法 | 路径 | 请求 | 响应要点 |
| --- | --- | --- | --- |
| GET | `/api/settings` | — | `fields{name:{label,secret,value,masked,has_value,source}}`、`overrides[]`、`llm{active,base_url,models}`、`creative{provider,has_key,...}`、`providers[]`、`creative_providers[]`、`env_file` |
| PUT | `/api/settings` | 上述字段任意子集（空串删覆盖） | 同 GET；校验失败 `400 invalid_setting`，空请求 `400 empty_update` |
| POST | `/api/settings/reset` | — | 清空覆盖，同 GET |
| POST | `/api/settings/test` | `{target:"chat"\|"creative", base_url?, api_key?, model?}` | 1 token 探活；`{ok, model, elapsed_ms, tokens, reason?}` |

`source` 三态：`db`（页面设置）/ `env`（.env）/ `none`（都没填）——前端用它打标签。

### 23.5 安全边界（五条）

1. **key 只进不出**：`GET` 永远不回明文，secret 字段的 `value` 恒为空串，只给 `masked`（≥12 字符露前 6 后 4，否则全星）。
2. **协议白名单**：`base_url`、`creative_adapter` 只接受 `http://` / `https://`，长度 ≤300。
3. **模型名**：不允许空白字符、≤120 字符（防奇怪字符串进请求体）。
4. **审计不记值**：只写 `settings.update fields=[...]` 到日志，值里可能有 key，绝不落日志。
5. **不落仓库**：`.env` 与 `*.db` 都在 `.gitignore`；页面上也提示「换电脑需重新填」。

### 23.6 前端

- 「设置」页签，两块：**对话模型**（服务商胶囊 → 自动填地址与候选模型，模型用 `datalist` 可下拉可手输）、**生图**（local / 百炼）。
- secret 输入框：placeholder 显示已存掩码 +「留空则不修改」，所以用户不会因为没重填而误删 key。
- 状态条：`对话模型：真实调用 / 模拟（规则版）`、`生图：本地 PIL / 百炼`、`已覆盖 N 项`。
- 底部按钮：保存 / 测试对话连接 / 测试生图 key / 恢复 .env 默认。

### 23.7 已知边界

- 覆盖是**按进程**读库的：多 worker 部署时各进程在下一个请求周期看到新值，不是分布式实时同步。
- key 在 SQLite 里是**明文**（未加密）：本机单人演示可接受；多租户部署要接 KMS/信封加密，不在本期范围。
- 不支持「不同任务用不同供应商」：planner / fast / vision 共用一套 `base_url` + key，只有模型名可分别设置。

---

### 22.7 第二轮复审（replan，A 接线后）

第一轮 Critic 只是「打分或丢弃」，被驳回的卡直接进了 `meta.dropped`，信息浪费。第二轮**只救已有 scope**：拿 14 天窗口重扫同一 scope，看能不能补到该根因需要的辅证指标。**不全局枚举新卡**——14 天窗口会把半年前的旧异常一起扫进来（实测 11-17 扩窗后冒出 8 月 R6 的 `SKU0077` 负评），主动制造误报。

| 动作 | 触发 | 影响 |
| --- | --- | --- |
| `upgrade` | 有必需辅证，且 14 天窗口内 `required_found=true` | 置信度抬到 ≥0.60，`critic.verdict` 改 support，caveat 记「复审：补到 X 辅证」 |
| `drop` | 有必需辅证但查不到；或 z 硬线不过（证据全部 `|z|<2.5`） | 移出列表，`meta.dropped` 记「第二轮复审撤销：…」 |
| `demote` | 该根因**无必需辅证**（R1/R2/R7/R8）且 z 硬线过 | 置信度 ×0.75（下限 0.2），保留卡 |
| `skip` | 失败检查只有 `promo` / `market`（口径问题） | 不改分数，只记录 |

三条硬约束：

1. **no-required 类永不因「scope 在宽窗里消失」被丢**——14 天窗口的促销日占比变了，scope 消失不等于异常不存在。第一版按「scope 没了就 drop」写，把 11-17 的 R7 与 11-11 的 R1 两个真命中误杀（召回 10→8）。
2. **复检出错必须降级、不能吞诊断**：`_run_replan` 任何异常只置 `degraded=true` 并原样返回卡片。
3. **预算 `REPLAN_BUDGET_MS=2500`**：一次 `detect` 0.7–1.0 s，超预算直接跳过，宁可少一轮也不拖垮 8 s 端到端。

实测（`data/store.db`，6 个候选日，全部走真实 `build_diagnosis`）：

| 日期 | 首轮卡 | 复审后卡 | 命中 | 精度 | 动作 |
| --- | --- | --- | --- | --- | --- |
| 08-18 | 5 | 1 | 1 | 100% | 4 drop（缺 stockout_rate / z 2.4、2.3） |
| 09-30 | 4 | 3 | 3 | 100% | 2 upgrade（补到 roi）、1 drop |
| 10-20 | 1 | 1 | 0 | 0% | —（检测层问题，见 22.6） |
| 10-27 | 1 | 1 | 0 | 0% | — |
| 11-11 | 3 | 3 | 2 | 67% | 1 demote（R1 保留） |
| 11-17 | 5 | 4 | 4 | 100% | 1 drop（R4）、1 demote（R7） |
| 合计 | 19 | **13** | **10** | **53% → 77%** | 召回 10/19 不丢，top-3 4/6 |

耗时：11-17 全链路 1849 ms（其中复检 844 ms）；08-18 1727 ms；10-20（无复检）854 ms。开关：`with_replan=False` / `GET /api/diagnose?replan=false`，用于消融。

### 22.8 检测层的促销口径与退款结构（B 轮修复）

`22.5` 的 6 日实测里 10-20 / 10-27 是 0 命中，当时记为「检测层滞后指标问题」。B 轮先验后修，结论是**促销口径过紧**，不是退款滞后。

**先验被否证**：对退款率做 0 / 5 / 7 / 10 天成熟期平移（`project/.tmp/b_lag_probe.py`），10-20 在 lag=0 时 `refund_rate` 已经是 `0.1140 → 0.1212（+6.3%）`，lag=5/7/10 反而 −0.8%~+3.3%。合成数据的退款 `dt` 紧贴下单日，不存在「新单未退款稀释」，**滞后补偿解决不了问题**。

**真因**：`PROMO_SKEW_THRESHOLD = 0.15` 意味着 7 天窗口里有一天大促（占比变化 1/7 ≈ 0.14…0.18）就触发全局降权——`score × 0.25` 且 `z_min` 从 2.0 提到 3.0。10-20 的 `promo_share` 是 window 0.18 / baseline 0.00，于是真实退款异常被一起压掉（只剩 7 个点）；10-27 是 1.0 / 0.28，73 个点全被压到 0 分。

| 修复 | 位置 | 口径 |
|---|---|---|
| 阈值放宽 | `PROMO_SKEW_THRESHOLD = 0.35` | 「一天大促」不再等于「全局不可比」；降权只在促销结构真的变了时发生 |
| 促销口径匹配（能力，默认关） | `DetectConfig.match_promo` / `min_matched_days=7` / `promo_lookback_days=56` | 窗口全在促销期时，向前找**同促销状态**的天做基线；按「天集合」算占比（不能用 min~max 跨度，会把中间非促销天算进去）；匹配成功 `skew=0` 不再降权 |
| 退款结构区分 | `hypothesis.refund_split` → `score(split=...)` | 总退款率涨 ≥15% 且尺码类 <10% → 更像 **R10 物流**；尺码类涨 ≥20% → 更像 **R4 描述不符**；否则不下结论 |

`match_promo` **默认关闭**，原因是实测：11-17 打开匹配后变成 70 个点（促销前后不可比的全量下滑），卡片由 R7 变成 R3/R4，再被第二轮复审的复检全部 drop → 1 卡 0 命中。所以它是「诊断促销期专用口径」的开关，不是默认路径。

另加 `meta.promo_dominated`（窗口促销日占比 ≥0.9 且基线 <0.3）：命中时每张卡落一条 caveat「本期整段都在促销活动内，基线几乎没有促销天，单周同比不可比，建议按促销期整体看」。

**实测（默认：匹配关 + 阈值 0.35 + 退款结构）**

| 日期 | 卡数 | 命中 | 精度 | top-3 | 卡片 |
|---|---|---|---|---|---|
| 08-18 | 1 | 1 | 100% | ✅ | R6 |
| 09-30 | 3 | 3 | 100% | ✅ | R5, R5, R2 |
| 10-20 | 2 | 1 | 50% | ✅ | R6, **R10**（修复前 0 张卡） |
| 10-27 | 1 | 0 | 0% | ❌ | R6（附促销期 caveat） |
| 11-11 | 3 | 2 | 67% | ✅ | R3, R1, R6 |
| 11-17 | 4 | 4 | 100% | ✅ | R3, R3, R7, R7 |
| 合计 | **14** | **11** | **79%** | 5/6 | 77% → 79%，有命中天数 4/6 → 5/6 |

**10-27 仍未解决（诚实边界）**：真值是 R1 + R10，窗口 10-21~10-27 整段在双 11 内，基线里只有 10-20 一天促销。要凑够匹配天数只能借 9-01~9-09（99 划算节）的促销天，属于「不同大促之间对比」，结果把 `SKU0064` 的退款尖峰判成 R4。另一条线索也断了：生成器 `REFUND_REASONS` 的权重是固定的（`LOGISTICS 0.13` / `SIZE_SMALL 0.42`…），退款原因分布**不随 R10 变化**，所以不能用 `reason_code` 直接区分 R4/R10，只能靠 `return_rate` 代理。

结论：**促销期单周不做归因**是当前数据下的正确取舍，产品上表现为「这一周请按促销期整体看」的 caveat，而不是硬凑一张卡。

## 24. 压测与部署（T10）

### 24.1 为什么压测

spec §3.8 写的验收线是「50 并发错误率 <1%、P95 ≤8s」。这个项目里 `POST /api/query` 是毫秒级，
但 `/api/diagnose` 单次 1.5~2.5s（600+ 格扫描 + 88 个异常点 + 4 张卡 + Critic + 复审），
并发一上来就会排队——**压测要证明的不是「功能对不对」，而是「演示时三个人同时点会不会卡死」**。

### 24.2 压测工具

`project/tools/load_test.py`（纯标准库 + httpx）：

| 套件 | 打哪些接口 | P95 线 |
|---|---|---|
| `light` | `/api/health`、`/api/query` | 3000ms |
| `mixed` | health / overview / query / playbook.search | 3000ms |
| `heavy` | `/api/diagnose?as_of=...`（最重的那条） | 8000ms |

用法：

```bash
python -m tools.load_test --suite heavy --concurrency 20 --requests 40
python -m tools.load_test --suite mixed --concurrency 30 --requests 240 --json --out .tmp/mixed.json
```

`--warmup 0` 关掉预热（测冷缓存），`--json` 出机器可读结果，退出码 1 表示越线。

### 24.3 首轮结果（没有缓存时）

| 套件 | 并发/请求 | P50 | P95 | RPS | 错误率 | 结论 |
|---|---|---|---|---|---|---|
| light | 20 / 200 | 61ms | 205ms | 246.8 | 0% | 通过 |
| mixed | 20 / 200 | 195ms | 2297ms | 25.5 | 0% | 勉强通过（overview 均值 1954ms） |
| heavy | 20 / 40 | 14617ms | 17360ms | 1.3 | 0% | **不通过** |

结论很直接：**错误率是 0，但重接口在并发下线性堆积**。`/api/diagnose` 单发 1.5s，
20 个并发一起打就是 17s——因为每个请求都在线程池里重算同一份结果。

### 24.4 修法：TTL 缓存 + single-flight

`project/app/core/cache.py`：

- `TTLCache(ttl_s=120, maxsize=64)`，进程内 LRU + TTL。
- 键 = `(db_path, round(db_mtime, 3), *业务参数)`——**数据重生成后 mtime 变了，缓存自动失效**，不需要手动清。
- `get_or_compute()` 带**单飞**：同键并发进来只有一个线程真算，其余在键锁上等，算完一起拿结果。
  没有这一步，缓存对「同时到达」的尖峰完全无效（20 个请求会各自 miss 一次）。
- `/api/overview` 与 `/api/diagnose` 都走它，返回体里加 `meta.cached`（true/false）便于验证命中。

踩过的坑：**uvicorn 的旧进程还占着 8000（`tools.dev` 用 SO_REUSEADDR 预绑定，两个进程能同时绑成功）**，
所以改了代码重启后请求仍打到旧进程，`meta.cached` 一直是 `None`。判断「跑的是不是新代码」最省事的办法
就是看响应里有没有新字段。

### 24.5 修后结果（同一台机器，`data/store.db` 82MB）

| 套件 | 并发/请求 | P50 | P95 | P99 | RPS | 错误率 |
|---|---|---|---|---|---|---|
| light | 20 / 200 | 64ms | 103ms | 122ms | 294.4 | 0% |
| mixed | 20 / 200 | 152ms | 538ms | 585ms | 92.0 | 0% |
| mixed | 30 / 240 | 294ms | 515ms | 598ms | 90.0 | 0% |
| heavy（冷缓存） | 20 / 40 | 2810ms | **2842ms** | 2842ms | 13.7 | 0% |
| heavy（热缓存） | 20 / 40 | 31ms | 56ms | 58ms | 526.9 | 0% |
| heavy（热缓存） | 30 / 60 | 51ms | 71ms | 73ms | 492.1 | 0% |

- 冷缓存 P95 从 **17360ms → 2842ms**（单飞把 20 个并发收敛成 1 次计算 + 19 次等待）；
- 热缓存 P95 **56ms**，比单发还快，因为直接读内存；
- 三套件 6 组配置**全部通过**，错误率 0%。

### 24.6 诚实边界

1. **单进程**。缓存是进程内字典，多 worker 会各存一份（功能正常、命中率下降）；真要横向扩，第一步是把缓存换成 Redis，不是加 worker。
2. **SQLite 写锁**。读接口能并发，`/api/actions/*` 的写操作靠 `BEGIN IMMEDIATE` 串行；演示够用，多商户不行（见 §21 的多租户留白）。
3. **缓存不区分租户**。键里只有 `db_path`，目前是单库单店；接多店时键要加 `store_id`。
4. **冷启动 2.8s 仍在 8s 内**，但它是「一个请求扛 20 个并发」的结果；如果并发的是 20 个**不同** `as_of`，P95 会回到 15s 量级——这属于设计约束（每个时间窗都要真算一次），不是 bug。

### 24.7 容器化

`project/docker/Dockerfile`（python:3.12-slim + 非 root 用户 + healthcheck）与
`project/docker-compose.yml`（挂载 `data/`，容器内不重建数据）。镜像**不含** 82MB 数据库与 300 张素材，
首次启动若发现库不存在，entrypoint 会跑 `python -m app.data.seed` 生成。

> 本机 Docker daemon 未启动，镜像构建未验证（`docker compose up` 需先开 Docker Desktop）。
> 这一步留在交付清单里：**能写、能解释、未构建**。

## 25. 创意工单（素材诊断 → 改进素材）

### 25.1 为什么做这件事

素材诊断（§19）能说出「主图文字占比 0.42、缺尺码表」，但**说完就停在结论上**。
商家真正想要的是「那你给我改一版」。这一节就是把「诊断」接到「产出」：
把问题码变成一张可复核的改进工单，再落回执行闭环（`swap_creative` 动作）。

三条边界：

- 不训练、不微调模型，也不做生视频（§2.2）。
- 工单本身**不生效**，只起草 `swap_creative` 动作（risk 1，人审后才执行）。
- 没有百炼 key 时自动降级为本地 PIL 改图，链路照样走通（`status="degraded"` 并写原因）。

### 25.2 数据契约

`app/creative/schema.py`：`CreativeRequest` / `CreativeConstraints` / `CreativeResult`。

| 字段 | 含义 |
|---|---|
| `issues` | 问题码，来自素材诊断；只接受 `NO_SIZE_CHART/TEXT_HEAVY/PLAIN_WHITE/WATERMARK/COLLAGE/NO_MODEL` |
| `constraints.max_text_coverage` | 改完文字占比上限，默认 0.28（合规检查用，容差 +0.02） |
| `constraints.add_size_chart` | 是否补尺码表（`NO_SIZE_CHART` 时必须为真才补） |
| `before` / `after` | 改前/改后的图 URL 与实测属性，**after 的 `text_coverage` 是重新量的**，不是照抄目标值 |
| `copywriting` | 标题 / 三个卖点 / 描述 / 尺码文案；图上文字变少，卖点就要进文案 |
| `compliance` | 逐项 `pass/warn/fail` + `blockers`；`ok=false` 时不允许起草动作 |
| `fixes` | 每个问题码一条，`status="manual"` 表示只能重拍（拼图、无模特） |

自动可改：`TEXT_HEAVY`（重排文字）、`NO_SIZE_CHART`（画尺码表）、`PLAIN_WHITE`（加场景底）、
`WATERMARK`（覆盖水印区）。只能人工：`COLLAGE`、`NO_MODEL`——工单会如实写「需要重拍」，不假装改好了。

### 25.3 两档 provider

| provider | 做什么 | 依赖 | 何时用 |
|---|---|---|---|
| `local` | 在现有合成图上用 PIL 重排/补表格/加场景 | 仅 Pillow | 默认；无 key；演示 |
| `dashscope` | 百炼图生图 `wan2.5-i2i-preview`（文生图 `wan2.2-t2i-flash`）重绘 | 百炼 key + 网络 | 设置页切到百炼且有 key |

- 选 `dashscope` 但 `available()` 为假 → 降级 local，`status="degraded"`，`note` 写明原因。
- 真调失败（超时/额度/内容审核）→ 同样降级 local，不让整张工单失败——**生图是加分项，不是主链路**。
- 生图是**异步任务式**（提交 `task_id` → 轮询 → 下载 URL），不走 OpenAI 兼容协议，所以单独适配器 `app/creative/dashscope.py`。

### 25.4 合规检查（硬门）

| 检查 | 判定 | 不过的后果 |
|---|---|---|
| `text_coverage` | 实测 ≤ 上限 + 0.02 | blocker |
| `size_chart` | 要求补且改后确有 | blocker |
| `watermark` | 要求去且改后确实没了 | blocker |
| `claims` | 标题/卖点里没有「最、第一、顶级、绝对、国家级、全网最低、永久」 | blocker |
| `manual_issues` | 拼图/无模特 → warn | 不拦，但工单标明需重拍 |

`service.apply()` 在 `compliance.ok=False` 时直接拒绝——宁可这单不能用，也不让不合规素材进审批队列。

### 25.5 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/creative/draft` | 生成工单；body 即 `CreativeRequest`；返回 `CreativeResult` + `meta.wall_ms` |
| GET | `/api/creative?sku_id=&limit=` | 工单列表（摘要，不含全文） |
| GET | `/api/creative/{draft_id}` | 单张工单全文 |
| GET | `/api/creative/asset/{draft_id}` | 取工单产出的图（`<img>` 直接用，路径限制在 `data/creative/` 下） |
| POST | `/api/creative/{draft_id}/apply` | 起草 `swap_creative` 动作（`params.creative_id=draft_id`、`image_path=after 图`） |

工单落 `creative_draft` 表（`draft_id/sku_id/provider/status/target/request_json/result_json/created_at`），
`apply` 之后能在执行台看到一条 `换素材` 待审动作，执行后 `store_state.creative` 变成该 `draft_id`，回滚则恢复。

### 25.6 与 AgenticCommerce 的关系

对接思路参考了开源项目 `haowlai/AgenticCommerce` 的 `AssetPackage / ComplianceReport` 结构，
但**没有引入它的代码**：它要 Postgres + pgvector + Redis + Vue + LangGraph，与本项目栈完全冲突。
这里只借「工单 = 素材 + 文案 + 合规」这个形状，实现是自包含的（SQLite + PIL + httpx）。

### 25.7 诚实边界

1. 本地改图是**规则重排**，不是生成模型：能压文字、补尺码表、加场景底，但不会「换一件更好看的裙子」。
2. 百炼出图**未做真机验证**（没有真实 key 与额度），适配器按官方文档实现，测试用 MockTransport。
3. 合规检查是**黑名单**，防不住新的绝对化话术变体；真正上线要接平台的素材审核接口。
4. 每张工单只改一个 SKU 的主图；详情页多图、短视频不在范围（§2.2）。

## 26. 数据接入：把商家的文件变成我们的表

### 26.1 为什么需要它

前 25 节的诊断全部建立在 `seed` 生成的合成数据上。对商家说的第一句话如果只能是
「我们先给你造一份数据」，这个产品就没法用。这一节解决的是**数据从哪来**：

```
商家导出文件（CSV / XLSX）
  → 认字段（别名库 + 内容形态）→ 人工确认映射
  → 质量闸（四条硬规则）→ 幂等写入 fact/dim 表
  → 现有 24 指标语义层（零改动）
```

一条硬约束：**接入层只做翻译，不做计算**。所有指标、视图、诊断都不感知「数据从哪来」，
所以接新平台只是往别名表里加几个列名。

### 26.2 交付形态：单商家单实例

按 toB 的现实（见 §21 的讨论结论）：

| 项 | 选择 | 理由 |
|---|---|---|
| 隔离 | **一商家一套实例 + 独立 `store.db`** | 数据不出商家环境，比多租户表设计更容易讲清楚 |
| 租户字段 | 不加 `tenant_id` | 一个实例只有一个商家；`store_id` 保留（一个商家可能多店） |
| 存储 | SQLite 单文件 | 单商家日单量 10 万以内足够；迁移 Postgres 的触发点写进 runbook |
| 部署 | 本地 / 单容器 | 当前阶段只在本地跑，不做离线安装包与多机编排 |

### 26.3 规范模型（canonical v1）

`app/ingest/canonical.py` 定义四个数据集，字段名与现有表一一对应：

| 数据集 | 必需字段 | 可选字段 | 没有它会怎样 |
|---|---|---|---|
| `orders` | `order_id` `dt` `sku_id` `pay_amount` | `qty` `unit_price` `discount` `status` `channel` `segment` | 什么都算不了 |
| `traffic` | `dt` `sku_id` + 至少一个信号列 | `impressions` `clicks` `detail_uv` `add_cart_uv` `pay_uv` `paid_orders` `gmv` | 曝光/点击类根因（R2/R7/R8）不可用 |
| `products` | `sku_id` `title` | 类目 / 吊牌价 / 成本 / 款式 / 面料 / 颜色 / 上架日期 | 维度下钻变粗、同行对比退化 |
| `refunds` | `order_id` `dt` | `refund_id` `sku_id` `amount` `reason` `status` | 退款率与 R4/R10 的区分不可用 |

「信号列」是 §26.4 的一个补充规则：只有日期和商品号、没有任何指标的流量表，
认出来也没用，所以算「不完整」。

字段别名是**数据**不是代码：`Field.aliases` 里既有中文平台列名（`买家实付金额`、`商家编码`、
`售后单号`），也有英文（`orderid`、`sku`、`total`）。加平台 = 加别名。

### 26.4 识别：别名 → 包含 → 内容形态

`app/ingest/detect.py` 分三级命中，分数即置信度：

| 级别 | 分数 | 例子 |
|---|---|---|
| 别名精确（归一化后相等） | 0.95 | `买家实付金额` → `pay_amount` |
| 别名包含 | 0.80 | `退款金额` 包含 `金额` → `pay_amount`（订单） |
| 内容形态 | 0.60 | 某列 90% 能解析成日期 → 可能是 `dt` |

选数据集不是「谁命中的列多」，而是：

1. 必需字段齐不齐（缺一个就不算完整）；
2. 有没有信号列（§26.3）；
3. 完整的前提下比字段覆盖率（退款表与订单表必需字段高度重叠，靠这个分开）；
4. 再比必需字段命中数。

这一步踩过的坑写在测试里：抖音流量表的 `曝光人数/点击人数` 一开始被「都是整数」的
启发式搞反了，所以别名表补齐了「人数」写法，并用 `test_detect_douyin_traffic_and_keeps_direction`
锁住方向。

### 26.5 质量闸（四条硬规则）

`app/ingest/quality.py`，任一硬规则不满足即 `reject`：

| 规则 | 阈值 | 为什么 |
|---|---|---|
| 必需字段可解析率 | ≥ 90%（`MIN_OK_RATE`） | 剩下 10% 允许是表尾合计行、空行 |
| 金额恒等式 | `单价×数量−优惠 ≈ 实付`，容差 `max(1, 0.5%)`，失败率 ≤ 5% | 对不上说明列错位或用了「应付/实收」混用，宁可挡住 |
| 重复率 | ≤ 2%（`MAX_DUPLICATE_RATE`） | 导出常见重复行，重复会把 GMV 算大 |
| 日期断档 | ≤ 2 天（`MAX_GAPS`） | 只警告不拒绝：商家可能真的停业 |

另外两条只警告：超过 5 条坏行给出前 5 条原因（`第 N 行：订单号为空`）；
行数或日期范围异常时在报告里说明，不阻断。

预览阶段就把「必需字段对不上」直接挡掉（`PreviewError`），不生成一个永远提交不了的 token。

### 26.6 入库：幂等 + 可回滚

`app/ingest/store.py`：

- 订单主键 `order_id`、流量主键 `(dt, sku_id, channel_id)`、退款主键 `refund_id`，
  全部 `ON CONFLICT DO UPDATE`——**同一份文件导入两次，第二次是 `inserted=0, updated=0, unchanged=N`**，
  这是 toB 场景最基本的要求（否则商家重跑一次报表就把成交额翻倍）。
- **不报错 ≠ 没变化**。三类会造成静默覆盖、必须显式处理的情况：
  1. **金额/数量被改动**：提交前先算 diff（新增 / 变更 / 无变化），有变更行时接口返回 **409 `changes_need_confirm`**，
     线上界面列出「哪一行、哪个字段、改前 → 改后」，商家确认后才写；差异只对「值真的不同」计数，
     金额比较容差 `0.01`。
  2. **退款没有单号**：不再用「行号」拼 id（行序一变就会重复插入），改为
     `RF + sha1(order_id|dt|amount|reason)[:12]`——同一笔记账来源的行序变化得到同一个 id；
     代价是「同单、同天、同额、同原因」的两笔退款会被合并成一条（可接受，退款明细一般有单号）。
  3. **批次可撤销**：提交时把每行的「改前值」压缩后写 `ingest_undo`（行数上限 2 万），
     `POST /api/ingest/undo` 倒序执行：新增的行 `DELETE`、改过的行 `UPDATE` 回原值，
     然后 `ingest_run.status='undone'`；已撤销的批次再撤会 409。商品行若已被事实表引用则跳过删除。
- 缺维度自动补：渠道匹配不到 → 建 `CH_OTHER`；商品没建过 → 补一行 `dim_sku`（价格带按吊牌价）；
  日期 → 补 `dim_date`（含周、月、促销标记占位）。
- 整个写入是**一个事务**：中途失败不会留下半份数据。
- 入库前 `sqlite3.backup()` 整库备份到 `.tmp/ingest/backups/store-<时间戳>.db`
  （不写 `data/`，那里 ACL 只允许追加式写入，见 runbook 的坑）。
- 每批写 `ingest_run`（`run_id/dataset/filename/file_sha256/rows_*/inserted/updated/changed/unchanged/status/verdict/mapping_json/report_json/backup_path`），
  导入失败可以对着批次号回查。
- `ensure_schema()` 用 `PRAGMA table_info` 逐列 `ALTER TABLE ADD COLUMN` 升级老库，
  所以旧实例升级代码后不用迁移数据。

`agg_daily` 只有 seed 生成器在用，视图与体检台都直接读事实表，所以导入后**不需要重建聚合**。

已知边界：`dim_date.is_promo` 对导入产生的新日期恒为 0，促销偏斜（R7）判定对新商家库会偏保守；
促销日历接入单独立项。

### 26.7 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ingest/schema` | 四个数据集、字段、别名、能算出的指标、可接受的扩展名 |
| POST | `/api/ingest/preview` | multipart：`file` + 可选 `dataset` / `mapping`(JSON)；返回识别结果、映射建议、质量报告、预览 token |
| POST | `/api/ingest/commit` | `{token, allow_warn, confirm_changes}`；写库并返回新增/变更/无变化、样例差异、是否可撤销、备份路径、最新能力清单；有变更未确认 → 409 `changes_need_confirm` |
| POST | `/api/ingest/undo` | `{run_id}`；撤销批次（删除新增、恢复改前值）→ 200；已撤销 → 409；未知批次 → 404 |
| GET | `/api/ingest/runs` | 最近批次（含 `changed/unchanged/status`） |
| GET | `/api/ingest/capabilities` | 已有数据集与缺失指标 |

预览产物落在 `.tmp/ingest/previews/<token>.json`（+ 原文件副本），提交只认 token，
所以商家在界面上的映射修改不会丢。

### 26.8 前端「数据导入」页

- 顶部是「当前数据」：七个数据集各一行，有数据显行数、没有显「缺」，并列出因缺数据算不出的指标。
- 中间：选文件 → 自动识别 → 映射表（规范字段 / 对应列 / 把握百分比；必填标红）→ 改一个列就重算报告。
- 质量报告：可入库 / 跳过 / 重复率 / 金额验算 / 日期范围 / 断档，`reject` 时按钮禁用。
- 底部：最近导入批次（时间、类型、文件、行数、新增/变更/无变化、结果、撤销）。
  有变更行时先弹差异面板（行键 + 字段「前值 → 后值」），确认后才写入；
  撤销是两段式：第一次点变「再点一次确认」，第二次才真的回滚。

### 26.9 实测

| 场景 | 结果 |
|---|---|
| 四份样例（淘宝订单 / 抖音流量 / Shopify 英文表头 / ERP 退款） | 全部识别正确，`verdict=pass`，映射含中文与英文列名 |
| 重复导入同一订单文件 | `inserted=0, updated=15`（GMV 不变） |
| 缺 `pay_amount` 的文件 | 预览阶段即 400，提示「缺少必需字段：实付金额」 |
| 坏行（订单号为空） | 跳过并计入 `rows_bad`，报告给出「第 N 行：订单号为空」 |
| HTTP 端到端（预览 → 提交 → 能力清单 → 批次） | 15 行 1.2s，诊断接口不受影响（4 张卡） |

### 26.10 诚实边界

1. 只支持 CSV / XLSX；没有 `pyarrow`，所以不读 parquet。
2. 平台 OAuth 直连（Shopify / 抖音 / 拼多多）与定时同步**未做**，属于 P1；当前靠商家导出文件。
3. 识别是别名 + 启发式，没有训练分类器；遇到完全陌生的表头仍然需要人工逐列确认。
4. 只做「增量 upsert」，不做删除同步：商家删掉历史订单，我们这边不会跟着删（需要时用回滚到备份）。
5. 多店（同实例多店铺）未做，`store_id` 先固定 `store_001`。

## 27. 登录鉴权（账号 / 角色 / 会话）

### 27.1 为什么现在补

前 26 节的所有接口都是「本机裸奔」状态：只有 CORS 白名单，任何能连到 8000 的人都能审批、执行、回滚、导入数据、改 key。
只要这台机器不是单人独占，这就是一个必须堵的口子——尤其「执行动作」和「改 key」两件事，出错的代价不可逆。

### 27.2 选型

| 选项 | 选择 | 理由 |
|---|---|---|
| 认证方式 | 账号 + 密码（scrypt） | 单人/小团队本地部署，不需要 OAuth 的复杂度 |
| 会话载体 | HttpOnly Cookie（`sd_session`，`SameSite=Lax`） | 同源部署，前端不需要存 token，也拿不到 token（XSS 偷不走） |
| 角色模型 | `staff < manager < owner` 三档 | 与真实店铺岗位对齐：运营 / 店长 / 店主 |
| API Key | 暂不做 | 没有对外集成方，先不引入第二套凭证体系 |
| 关闸开关 | `AUTH_MODE=off` | 自动化测试与本机单点调试需要放行，但不能出现在默认配置里 |

### 27.3 数据模型

```sql
users(user_id PK, username UNIQUE, display_name, role, password_hash,
      disabled, must_change, created_at, last_login_at)
sessions(token_hash PK, user_id, created_at, expires_at, last_seen, user_agent)
```

- `password_hash` 形如 `scrypt$16384$8$1$<salt>$<hash>`（`n=2^14, r=8, p=1, dklen=32`），校验用 `hmac.compare_digest`；
- 会话只存 `sha256(token)`，库泄露也拿不到可用的 Cookie；
- `must_change=1` 的账号登录后只能改密（前端强制，管理员重置密码也会重新置 1）。

### 27.4 拦截与权限

中间件对 `/api/*` 统一要求登录，白名单只有 `/api/health`、`/api/auth/login`、`/api/auth/whoami`；
未登录一律 401 `{"error":"unauthorized","message":"请先登录"}`，前端收到任何 401 都会广播回登录页。

| 能力 | 最低角色 |
|---|---|
| 查数、诊断、问诊、看链路、起草动作 | 登录即可 |
| 审批 / 执行 / 驳回 / 回滚、创意工单转动作 | `manager` |
| 设置（含 key）、数据导入 / 撤销、账号管理 | `owner` |

审计里的「操作人」不再取前端传值，而是取会话用户（`actor_of`）；`AUTH_MODE=off` 时才回落到请求体，这也是历史上出现 `t9` 这种测试代号的根源。

### 27.5 首次启动与改密

- 库里没有任何账号时，`lifespan` 自动创建 `admin`（owner）；
- 初始密码来自 `AUTH_ADMIN_PASSWORD`，没配就随机生成并写 `.tmp/first-run.txt`（同时打印到日志）；
- 初始密码只用于第一次登录：`must_change=1`，前端在进主界面前就要求改密；
- 改密后踢掉该用户的其它会话（当前会话重签保留）。

### 27.6 防爆破与停用

- 同一用户名 5 分钟内连续失败 5 次 → 锁 5 分钟（内存计数，重启清零，够本地部署用）；
- 停用账号时立刻作废它的全部会话（`end_user_sessions`），已登录的下一页就会 401；
- 不允许停用/降级自己（前端禁用按钮，后端也拒绝给自己降级）。

### 27.7 实测

| 场景 | 结果 |
|---|---|
| 未登录访问 `/api/overview` | 401「请先登录」 |
| `/api/health` | 200（公开，供探活与容器健康检查） |
| `admin` 用初始密码登录 | 200，`must_change=true`，Set-Cookie `sd_session` |
| 改密后旧密码登录 | 401 |
| `staff` 访问 `/api/settings` / `/api/ingest/preview` / 审批 | 403「权限不足：这一步需要「店长」及以上」 |
| `owner` 管理账号 | 200；`staff` 调 `/api/auth/users` → 403 |
| 连续 5 次错密码 | 第 6 次提示「登录失败次数过多，请 5 分钟后再试」 |

前端：登录页 / 改密页与主应用分离；未登录时连导航都不渲染；导航按角色过滤（设置与数据导入只对店主出现）；
动作抽屉对无审批权的角色只显示「这一步需要店长及以上权限」，不显示可点的按钮。

### 27.8 诚实边界

1. Cookie 默认不是 `Secure`（本机 http）；上 HTTPS 时要设 `COOKIE_SECURE=true`。
2. 没有 CSRF token：依赖 `SameSite=Lax` + 全部写操作都是 `POST/PATCH`，跨站表单拿不到 JSON 内容类型；上公网前应再加 CSRF 或改用双提交 Cookie。
3. 没有密码复杂度策略（只要求 ≥6 位），也没有「忘记密码」自助流程——本地部署由店主重置。
4. 失败计数在进程内存里，多进程/多副本部署会各算各的；本地单实例不受影响。
5. 没有 API Key / OAuth 客户端凭证，第三方系统目前无法直连（要接就再加一层）。

## 28. 促销日历

### 28.1 问题

系统里所有「促销」判断都收敛到一个字段：`dim_date.is_promo`。

- 诊断的 `promo_share`（大促日订单占比）与 `promo_skew` 读它 → 决定「窗口是不是在促销、基线是不是也在促销」；
- R7（大促透支）的假设加权读 `promo_skew`；
- Critic 的「量价不可比」检查读 `promo_skew`；
- 查数的维度「是否促销」直接映射 `is_promo`。

种子数据里这个字段是生成器写的（618 / 99 划算节 / 双 11，共 50 天），
但**导入的真实订单日期一律写成 `is_promo=0`**：商家把「双 11」订单导进来，系统只当它是普通的一天。
后果是 `promo_skew` 恒为 0 —— R7 永远不被识别，Critic 也不会提示「量价不可比」。

### 28.2 设计：日历是唯一真源

新增 `promo_calendar` 表，商家把自己知道的促销期画出来：

```sql
promo_calendar(promo_id PK, name, kind, start_dt, end_dt, note, created_at)
```

- `kind`：`campaign` 平台大促 / `store` 店铺活动 / `other` 其它；
- 一段最长 **90 天**（再长不像促销期，多半是填错），日期必须 `start_dt <= end_dt`；
- 写入后立刻调用 `rebuild_flags()` 把日历**展开写回** `dim_date.is_promo / promo_name`，
  语义层、查询维度、诊断、Critic 全部零改动——它们仍然只读 `dim_date`。

### 28.3 三条保守规则

1. **空日历不动存量**：`rebuild_flags()` 在日历为空且未显式 `force` 时直接跳过。
   否则第一次跑就会把生成器写好的 50 个演示促销日抹平。
2. **删除要清干净**：删掉最后一段日历时，用被删的范围 `clear_range()` 兜底清零，
   避免「日历里没有了，但 `is_promo` 还挂着 1」。
3. **导入只重算本次日期**：`commit()` 成功后按本批数据的 `dt` 范围重算，
   不碰其它日期；促销重算失败只记录原因，不能让导入失败。

### 28.4 接口

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/promo` | 登录 | 日历 + 覆盖范围 + 当前促销天数 |
| POST | `/api/promo` | 店主 | 新增一段，返回 `{period, sync}` |
| PATCH | `/api/promo/{id}` | 店主 | 改名称/类型/起止，旧日期会被清掉 |
| DELETE | `/api/promo/{id}` | 店主 | 删一段，同时清掉该段标记 |
| POST | `/api/promo/rebuild` | 店主 | 手动全量重算（一般不用） |

错误：`400 invalid_period`（名称空、日期倒置、超 90 天、类型非法）、`404 promo_not_found`。

### 28.5 界面

「设置 → 促销日历」（店主可见）：期间表（名称 / 类型 / 起止 / 天数 / 删除）+ 新建表单（名称、类型、起止）；
顶部一行说明「它决定促销日占比、R7 能否被识别、量价对比是否可信」；
每次写操作后提示「已重算 N 天，其中 M 天标为促销」。

### 28.6 实测

| 场景 | 结果 |
|---|---|
| 演示库收编（`adopt_existing`） | 618 / 99 划算节 / 双 11 三段，50 个促销日，与原来完全一致 |
| 新建 12-01~12-02 | 201，重算 185 天，促销日 50→52 |
| 删除该段 | 200，促销日回到 50 |
| 导入 11-20 的订单 + 日历覆盖该日 | `commit.promo.rebuilt=true`，`dim_date` 该日 `is_promo=1` |
| 改期（11-20~11-22 → 11-22~11-23） | 旧的两天被清回 0，只留新范围 |
| 日历清空后重算 | `rebuilt=false`，演示标记保持 50 天不变 |
| 非店主调用写接口 | 403（`require_role("owner")`） |

**敏感性实测**（`project/.tmp/promo_effect.py`，库副本）：
把「双 11 返场 11-12~11-18」标进日历后，窗口促销日占比 0.29 → **1.00**，`promo_skew` -0.57 → **+0.13**，
两张 R7 卡（0.58 / 0.38）消失，换成 R3 + R8 + R4。反过来也一样：
商家该标不标，系统就会把促销期的量价波动当成「真实下滑」。

### 28.7 诚实边界

1. 日历是**商家声明**，不是系统推断：不填就没有，填错结论就会偏（上面那张敏感性表就是证据）。
2. 只支持按天标记，没有「只在某个渠道/某些 SKU 促销」的粒度。
3. 跨午夜的促销（比如 12-31 20:00 到 01-01 02:00）只能按整天算。
4. 同一天被两段覆盖时，取「先开始的那段」名称；不报冲突（真实大促常有重叠）。
5. 没有节假日自动推荐、没有平台促销数据回填（那需要平台授权，见 §21）。
