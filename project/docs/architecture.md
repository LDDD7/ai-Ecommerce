# 架构说明

> 这份文档回答一个问题：**这个东西是怎么拼起来的，为什么这么拼。**
> 想跑起来看 [runbook.md](./runbook.md)；想调接口看 [api.md](./api.md)；想改代码看 [development.md](./development.md)。
> 完整设计（含取舍、阈值依据、已知边界）在 [spec](./superpowers/specs/2026-11-18-storedoctor-design.md)，本文是它的「可读版摘要」，两者不一致时以 spec 为准。

## 1. 一句话

店诊 Agent 是一个**单商家、本地部署**的店铺经营诊断系统：把「查数 → 归因 → 策略 → 审批 → 执行 → 复盘」串成一条可审计的链路，其中「执行」永远是 mock（写 `store_state`，不动真实平台）。

## 2. 分层与边界

```
                ┌───────────────────────────────┐
  商家/运营 ───▶ │  web（React，八视图）           │
                └───────────────┬───────────────┘
                                │ HTTP（Cookie 会话）
                ┌───────────────▼───────────────┐
                │  api 层（FastAPI 路由）         │  ← 鉴权中间件、参数校验、错误码
                └───────┬───────────────┬───────┘
                        │               │
        ┌───────────────▼──┐      ┌─────▼──────────────────────────┐
        │ 语义层 metrics     │      │ 领域层                          │
        │ QuerySpec→SQL     │      │ diagnose / playbook / vision    │
        │ 只读、参数化、看门狗│      │ retrieval / creative / promo    │
        └───────────────┬──┘      └─────┬──────────────────────────┘
                        │               │
                ┌───────▼───────────────▼───────┐
                │ 数据层 SQLite（WAL，单文件）    │  ← 事实表 / 维表 / 系统表
                └───────▲───────────────▲───────┘
                        │               │
              ┌─────────┴──┐     ┌──────┴─────────┐
              │ ingest 接入 │     │ actions 执行    │
              │ 只写事实/维 │     │ 唯一写业务状态   │
              └────────────┘     └────────────────┘
```

三条硬边界，代码里是这么守的：

| 边界 | 规则 | 落在哪 |
|---|---|---|
| **读与写分离** | 诊断、查数、检索只读；写库只有 `ingest`（导数据）和 `actions`（执行动作）两个入口 | `metrics/compiler.py` 用 `mode=ro` + `PRAGMA query_only`；`actions/execute.py` 是唯一改 `store_state` 的地方 |
| **模型不改事实** | 模型只能产「计划」「解释」「反方意见」，数字与置信度由规则算 | `orchestrator/planner.py` 白名单校验、`narrator.py` 数字白名单、`critic_llm.py` 置信度地板 |
| **结论必须带证据** | 没有证据的假设不进诊断卡 | `diagnose/evidence.py`、`diagnosis.py` 的「证据不全即丢卡」 |

## 3. 模块地图

| 目录 | 行数量级 | 职责 | 关键文件 |
|---|---|---|---|
| `app/api/` | 1.3k | 路由与 DTO：主接口、动作、创意、促销、鉴权 | `routes.py`、`actions_routes.py`、`auth_routes.py`、`promo_routes.py` |
| `app/core/` | 1.6k | 配置、LLM 网关、鉴权、权限守卫、缓存、运行时覆盖层、日志 | `config.py`、`llm.py`、`auth.py`、`guards.py`、`cache.py` |
| `app/metrics/` | 0.6k | 24 指标目录、5 个只读视图、QuerySpec 校验、SQL 编译器 | `catalog.yaml`、`views.py`、`spec.py`、`compiler.py` |
| `app/diagnose/` | 3.9k | 七件套：异常检测、GMV 分解、维度贡献、漏斗、证据、假设、诊断卡；Critic 与复审 | `anomaly.py`、`decompose.py`、`contribution.py`、`hypothesis.py`、`diagnosis.py`、`critic.py`、`replan.py` |
| `app/playbook/` | 0.7k | 27 张策略卡 + 检索 | `cards/PB-R*-NN.yaml`、`search.py` |
| `app/retrieval/` | 1.1k | FTS5(trigram) + bge-m3 向量 + RRF + 三级重排 | `fts.py`、`embed.py`、`rrf.py`、`rerank.py` |
| `app/vision/` | 1.3k | 主图属性抽取（mock/vlm）、同行对比、素材问题折算 | `inspector.py`、`compare.py`、`material.py` |
| `app/creative/` | 1.8k | 创意工单：本地改图 / 百炼生图、文案、合规 | `local.py`、`dashscope.py`、`service.py` |
| `app/actions/` | 1.8k | 五类动作：定义、校验、mock 执行、回滚、审计 | `schema.py`、`execute.py`、`store.py`、`plan.py` |
| `app/ingest/` | 1.7k | 数据接入：canonical 字段、表头识别、质量闸、幂等入库、撤销 | `canonical.py`、`detect.py`、`quality.py`、`store.py` |
| `app/orchestrator/` | 1.7k | 意图、规划（规则/模型）、工具白名单、报告、SSE 执行器 | `intents.py`、`planner.py`、`runner.py`、`narrator.py` |
| `app/promo/` | 0.4k | 促销日历 → `dim_date.is_promo` | `store.py` |
| `app/query/` | 0.3k | 自然语言 → QuerySpec（模型出 JSON、规则兜底） | `nl.py` |
| `app/data/` | 0.8k | 合成数据生成与注入根因 | `generator.py`、`seed.py` |
| `app/eval/` | 0.7k | 四个评测套件 + 门禁 | `suites/`、`gate.py` |
| `web/src/` | 5.8k ts + 5.7k css | 八视图 + 共用文案 | `views/`、`api*.ts`、`audit-labels.ts` |

## 4. 六条主链路

### 4.1 查数（结构化）

```
QuerySpec{metrics, dimensions, time, filters, order_by, limit}
   └─ spec.py 校验（字段/枚举/上限）→ compiler.py 拼参数化 SQL
        └─ 只读连接 + 8s 看门狗 + MAX_ROWS 5000 → rows
```

未知字段是**硬错误**（`400 invalid_spec`，提示「未知字段「dims」；只接受 …」）。曾经静默忽略，结果把「按天」查成「全量汇总」还返回 200，这条是专门修过的。

### 4.2 诊断（核心）

```
overview（健康分）
   └─ anomaly：7 天 vs 28 天双窗口，泊松下限 + 两比例 z 检验，促销偏斜降权
        └─ decompose：GMV = uv × cvr × aov，对数增量 + 链式归因（残差 0）
             └─ contribution：维度贡献（比率指标用两期加权分解）
                  └─ hypothesis：R1–R10 打分
                       └─ evidence：每张卡带 N 条证据（指标/维度/基线/当期/变化/z）
                            └─ critic：七项检查 → 置信度与反方意见
                                 └─ replan：二轮复审（升级/降级/丢弃/跳过）
                                      └─ cards（按影响金额排序）
```

诊断卡一旦被 Critic 驳回就不进展示列表，但会记在 `meta.critic.rejected` 里——**不吞结论，也不假装没有过**。

### 4.3 问诊（编排 + SSE）

```
POST /api/chat  {question, as_of, window_days}
   └─ intents：关键词投票定意图
        └─ planner：规则版或模型版（工具白名单 ≤4 步，缺前置自动补）
             └─ 逐个工具执行（overview / diagnose / decompose / playbook.search / action.draft）
                  └─ narrator：把事实压成 200~420 字报告（数字白名单校验，不过就回退模板）
                       └─ SSE：plan → tool_call → tool_result → diagnosis → evidence
                                  → critic → playbook → action_draft → token → done
```

事件流是**增量**的：用户先看到计划和工具在跑，再看到卡和报告。报告有 7s 预算，超了就跳过改写、用模板（宁可少润色，不能超时）。

### 4.4 素材诊断

```
diagnose（SKU 级卡）
   └─ inspector：mode=mock 读 image_feature 真值；mode=vlm 走视觉模型（detail=low）
        └─ derive_flags：flag 一律由属性重算（不信模型自报）
             └─ compare：同款式 Top10 中位对比
                  └─ material：六个问题码 → 健康分 → 挂到卡上
```

**教训**：首轮真机评测 0/10，不是模型不行，是合成图和标签不一致。重画 300 张图之后，字段级准确率 88.3%（`tools.vision_eval.py`）。

### 4.5 数据接入

```
文件（CSV/XLSX）
   └─ loaders：编码探测、分隔符嗅探、表头定位
        └─ detect：别名精确 0.95 / 包含 0.80 / 内容形态 0.60 → 选数据集 + 字段映射
             └─ quality：可解析率、金额恒等式、重复率、断档 → pass/warn/reject
                  └─ preview（落 token，不动库）
                       └─ commit：diff（新增/变更/无变化）→ 有变更要 409 确认
                            └─ 单事务写入 + 整库备份 + ingest_run + 可撤销
```

### 4.6 动作闭环

```
draft（生成待审动作，带风险等级与预期影响）
   └─ approve / reject（角色门槛：risk 2 需店主）
        └─ execute：mock 改 store_state，写 diff
             └─ rollback：按审计里的 revert 回滚
                  └─ audit_log / trace / 链路页
```

## 5. 关键阈值（改之前先看这里）

| 位置 | 常量 | 值 | 为什么 |
|---|---|---|---|
| `diagnose/anomaly.py` | 窗口 | 7d vs 28d | 周内波动被拉平，又能跟上变化 |
| `diagnose/anomaly.py` | 促销降权 | `PROMO_SKEW_THRESHOLD=0.35` | 0.15 时把正常波动也压掉，误杀真实下滑 |
| `diagnose/critic.py` | 支持/驳回 | `0.55 / 0.30` | 0.55 以上才叫「成立」，0.30 以下直接丢 |
| `diagnose/critic_llm.py` | 置信度地板 | `LLM_CONFIDENCE_FLOOR=0.8` | 模型最多压 20%，否则把 0.83 压成 0.55，排序失效 |
| `orchestrator/narrator.py` | 报告预算 | `REPORT_BUDGET_MS=7000` | 端到端 8s 验收线 |
| `core/llm.py` | 思考链 | `LLM_THINKING=off` | 开思考 8718ms/644 tokens，关掉 2162ms/279 tokens |
| `metrics/spec.py` | 上限 | metrics≤6、dims≤3、filters≤8、≤2 排序、limit≤5000、跨度≤400d | 防模型把库拖死 |
| `ingest/quality.py` | 质量闸 | 可解析≥90%、恒等式失败≤5%、重复≤2%、断档≤2 | 宁可拒收，不收坏数据 |
| `vision/inspector.py` | 视觉档位 | `detail=low` | 一张 800×800 足够判属性，成本差 3~5 倍 |

## 6. 设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | SQLite 单文件，不上 PG | 单商家单实例，量级（20 万单）SQLite 完全够；备份就是拷文件 |
| D2 | 前端 React，不用组件库 | 界面要苹果风，组件库反而更难压样式 |
| D3 | API-first，前端只调接口 | 同源部署、可被脚本调用、演示时能直接用 curl |
| D4 | 合成数据 + 注入根因 | 没有真实商家数据，`anomaly_gt` 给唯一真值，才能算准确率 |
| D5 | 显式状态机，不用 LangGraph | 五步链路，状态机可读可测；图上加节点反而黑箱 |
| D6 | 执行全 mock | 安全边界：演示系统绝不碰真实店铺 |
| D7 | OpenAI 兼容网关 | 一家一个 key，不绑死厂商 |
| D8 | 素材诊断用「属性 + 规则」，不端到端 | 可解释、可评测、可回放 |
| D9 | 检索 FTS + 向量混合 + RRF | 策略卡只有 27 张，纯向量会漏精确词 |
| D10 | Critic 先规则后模型，默认关模型 | 规则版已经把精度 33%→53%，模型版贵且慢 |
| D11 | 鉴权用 Cookie 会话，不做 API Key | 单机同源，Cookie 最省事；开放 API 时再补 |
| D12 | 促销日历是唯一真源 | 导入文件里没有「这天是不是促销」，不能让模型猜 |
| D13 | 导入要 diff + 可撤销 | 「不报错」不等于「没覆盖」，商家数据不能悄悄改 |

## 7. 不做什么（明确排除）

- 不做真实平台写入（广告、改价一律 mock）；
- 不做多租户、不做分布式、不做消息队列；
- 不做实时流（分钟级同步不在范围内）；
- 不做「自动执行高风险动作」（risk 2 必须人审）；
- 不承诺诊断准确率（当前 top-3 0.52，见 [eval-report.md](./eval-report.md)）。

## 8. 性能画像（本机，82MB 库 / 196,213 单）

| 操作 | 冷 | 热（TTL 120s） |
|---|---|---|
| `/api/overview` | 364ms | ~10ms |
| `/api/diagnose`（4 卡 + 素材） | 2.1s | ~50ms |
| `/api/query`（7 行） | 15~213ms | — |
| `/api/chat`（模型规划 + 报告） | 5.8s | 1.8s |
| 压测 heavy P95 | 2.84s（修缓存前 17.4s） | 56ms |

详见 [load-report.md](./load-report.md)。
