# StoreDoctor · 电商店铺经营诊断与动作闭环 Agent

> 一个能自己查数、归因、给方案、等人批准、执行、再复盘的店铺经营 Agent。
> 不是又一个「问问 RAG」——它要能**指出问题、拿出证据、动手执行**。

**当前进度**（2026-11-21）：W1 / W2 / W3 主体完成。合成数据（300 SKU / 180 天 / 19.6 万单 / 10 类注入根因）+ 24 指标语义层 + 诊断引擎（含 Critic 反方审查、两轮复审）+ 27 张策略卡 + 混合检索 + LLM 网关与规划器 + 报告改写与数字校验 + 动作闭环（审批 / 执行 / 回滚 / 审计 / 链路）+ 素材诊断与创意工单 + 数据导入 + 登录鉴权。
**后端测试 633 项全绿**；前端八视图（体检台、诊断与证据、问诊、查数、数据导入、执行台、链路、设置）已写完，`tsc --noEmit` 通过，`npm run dev` 需本机启动。**查数**支持两种入口：结构化勾选（指标 / 维度 / 过滤条件 / 时间窗，零 token）+ 自然语言（「近 14 天成交额和转化率按天看」→ QuerySpec，有 key 时模型解析、无 key 规则解析，模型永远拿不到写 SQL 的机会）。**压测已过**：20/30 并发下三套件（轻 / 混流 / 重接口）P95 全部达标，`/api/diagnose` 冷缓存 P95 从 17.4s 降到 **2.84s**、热缓存 56ms、错误率 0%（[压测报告](project/docs/load-report.md)）。**创意工单**已通：诊断卡上的「生成改进素材」→ 本地 PIL 改图（或百炼生图，无 key 自动降级）→ 文案 + 合规检查 → 一键起草换素材动作进审批队列。

---

## 快速开始（三条命令）

```powershell
cd E:\llm\third\Ecommerce\project
python -m tools.dev                      # 起后端 8000（会提示前端怎么起）
python -m tools.check_all                # 离线自检：测试 + 策略卡 + 素材评测 + 环境（不花额度）
```

前端另开一个终端：`cd web; npm run dev` → 打开 http://localhost:5173 。

**首次打开会要求登录**，账号 `admin`：

- 全新库：启动时若没配 `AUTH_ADMIN_PASSWORD`，会随机生成密码并写到 `project/.tmp/first-run.txt`，首次登录必须改密；
- 本仓库自带的演示库：密码已经改成 `StoreDoctor2026`（`must_change=0`），可以直接登；
- 换库/重 seed 之后按第一种情况走。

### 角色与权限

| 角色 | 能做什么 |
|---|---|
| 运营 `staff` | 查数、看诊断、问诊、让 Agent 起草动作、看链路 |
| 店长 `manager` | 上一档 + 审批、执行、驳回、回滚 |
| 店主 `owner` | 上一档 + 设置（含 key）、数据导入与撤销、促销日历、账号管理 |

登录态是 HttpOnly Cookie（页面拿不到 token，也不存 token）；密码用 scrypt 哈希；同一账号 5 分钟内连错 5 次会被锁 5 分钟。本机调试可设 `AUTH_MODE=off` 关掉鉴权（接口全部放行，仅用于自动化测试）。

完整步骤、配置项、评测命令、演示脚本、已知坑：[运行手册](project/docs/runbook.md)。

## 它解决什么问题

女装店铺运营每天要回答三个问题：**为什么掉了？是哪个 SKU / 渠道 / 环节掉的？现在该动什么、动了会怎样？**

传统 BI 停在看板，通用 chatbot 停在给一段分析。这个项目做完整闭环：

```
巡检发现异常 → 拆解归因 → 定位到证据 → 匹配策略卡 → 人工审批 → 执行(mock) → 复盘
```

## 四个设计取舍

| 取舍 | 选择 | 理由 |
|---|---|---|
| Agent 是否直接写 SQL | **否**，只输出 `QuerySpec`，由语义层编译 | 口径唯一、防注入、可审计 |
| 是否让 Agent 自己改价 | **否**，人审闸门 + dry-run + 幂等 + 可回滚 | 真实业务红线，也是岗位 JD 关键词 |
| 是否用 LangGraph | **否**，显式状态机 | 编排要能在 trace 面板逐帧回放，黑盒反而碍事 |
| 诊断结论要不要被质疑 | **要**，Critic 先规则后模型 | 只给结论不给反方意见的诊断，不可信 |

## 目录结构

```
project/                # 项目本体
  app/
    core/config.py      # pydantic-settings 配置（全部环境变量）
    core/llm.py         # OpenAI 兼容网关：重试、缓存、关思考、结构化 JSON
    core/auth.py        # 账号 / 角色 / 会话（scrypt + HttpOnly Cookie）
    core/guards.py      # require_role / actor_of（审计取登录用户）
    diagnose/           # 异常检测 / 分解 / 贡献度 / 漏斗 / 证据链 / 根因假设 / 诊断卡 / Critic / 复审
    playbook/           # 27 张策略卡（YAML）+ 严格 schema + 检索
    retrieval/          # FTS5(trigram) + bge-m3 + RRF 混合召回 + 三级重排
    orchestrator/       # 意图 → 计划（rule-v1 / llm-v1）→ 工具 → 报告（模板 / 模型改写）→ SSE
    vision/             # 素材诊断：属性抽取（mock 回放 / VLM）、同行对比、问题折回根因
    ingest/             # 数据接入：canonical 字段 + 表头识别 + 质量闸 + 幂等入库 + 批次撤销
    creative/           # 创意工单：问题码 → 改图（本地 PIL / 百炼）→ 文案 → 合规 → 起草动作
    actions/            # 五类动作：schema / 护栏 / 幂等 / 执行 / 回滚 / 审计
    query/              # 自然语言 → QuerySpec（模型出 JSON，规则兜底）
    data/               # schema.sql + generator.py + seed.py
    metrics/            # catalog.yaml（24 指标）+ views + spec + compiler
    insights/overview.py# 体检台概览：健康分、异常卡、30 天趋势、款式分布
    api/                # routes.py / actions_routes.py / creative_routes.py / auth_routes.py
  tools/                # dev / check_all / load_test / vision_eval / rerender_images …
  tests/                # 633 项 pytest（全部离线，不打真模型）
  samples/              # 四份仿真导入样例（淘宝订单 / 抖音流量 / Shopify 商品 / ERP 退款）
  docs/                 # 六份项目文档（架构 / 接口 / 数据模型 / 开发 / 运行 / 设计）+ 评测与压测报告
  docker/               # Dockerfile + entrypoint（镜像未在本机构建验证）
  web/                  # React 18 + Vite 7 + TS 5.9（八视图）
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务 / 数据 / LLM 配置状态，缺库时返回 degraded + 提示（**不需要登录**） |
| POST | `/api/auth/login` | 登录（账号密码 → HttpOnly Cookie `sd_session`；失败 401，5 次/5 分钟锁定） |
| POST | `/api/auth/logout` | 退出并作废当前会话 |
| GET | `/api/auth/me` | 当前登录用户（角色、是否需改密） |
| POST | `/api/auth/password` | 改密（改完踢掉其它会话） |
| GET/POST | `/api/auth/users` | 账号列表 / 新增（仅店主） |
| PATCH | `/api/auth/users/{user_id}` | 改角色、停用、重置密码（仅店主） |
| GET | `/api/metrics/catalog` | 指标目录与查询限制 |
| POST | `/api/query` | 执行 `QuerySpec`，返回 columns / rows / meta；未知字段是硬错误 400 `invalid_spec` |
| POST | `/api/query/nl` | 自然语言 → `QuerySpec` 并直接返回结果（`source=llm\|rule`，解析结果 120s 缓存） |
| GET | `/api/overview?days=7&as_of=` | 体检台概览：健康分、异常卡、趋势、款式分布 |
| GET/POST | `/api/diagnose?critic=rules\|llm&material=true&replan=true` | 一次完整诊断：诊断卡（根因 + 证据 + 反方意见 + 素材块 + 第二轮复审标记）+ GMV 分解 + 漏斗 |
| POST | `/api/chat` | 对话：SSE 流式（plan / tool_call / tool_result / diagnosis / evidence / critic / playbook / action_draft / token / done / error） |
| POST | `/api/playbook/search` | 策略卡检索：自然语言走混合召回；只给 `root_causes` 时按根因直达 |
| GET | `/api/llm/probe` | 探活：真发一次最小请求，回报模型名、延迟、token；无 key 返回 `ok:false` |
| GET | `/api/images/{sku_id}?role=main` | 主图原图（合成 PNG），诊断卡素材块直接引用 |
| POST | `/api/vision/inspect` | 素材检查：属性（文字占比 / 模特 / 场景 / 尺码表 / 水印 / 拼接）、问题码、证据、图片地址 |
| GET | `/api/settings` | 当前生效配置：字段、掩码后的 key、来源（`.env` / 页面设置）、服务商预设（仅店主） |
| PUT | `/api/settings` | 保存设置（key / 地址 / 模型），空串=删除该项覆盖；校验失败 400 `invalid_setting` |
| POST | `/api/settings/reset` | 清空页面覆盖，回到 `.env` 默认 |
| POST | `/api/settings/test` | 用 1 个 token 验 key 是否可用（对话 / 生图各一路） |
| GET | `/api/actions/types` | 五类动作的字段说明、风险分级规则、护栏阈值 |
| POST | `/api/actions/dry-run` | 只预览不落库：改前 / 改后、风险等级、护栏警告 |
| POST | `/api/actions` | 起草动作（校验 + 护栏 + 风险 + 幂等），落 `drafted` |
| POST | `/api/actions/draft` | 从诊断卡 + 策略卡批量起草（问诊内部也走这条） |
| GET | `/api/actions?status=&type=&limit=` | 动作清单：`count` 筛选命中数、`total` 全量数、`status_counts` 跟随类型过滤 |
| GET | `/api/actions/{id}` | 单个动作详情（含 payload、审计摘要） |
| POST | `/api/actions/{id}/approve` | 审批（需店长；risk 2 需店主）；`/reject` 拒绝 |
| POST | `/api/actions/{id}/execute` | 执行（原子抢占 + mock 改 `store_state`）；`/rollback` 回滚 |
| GET | `/api/audit?action_id=&limit=` | 审计事件（时间倒序） |
| GET | `/api/trace/{trace_id}` | 一轮问诊的诊断 → 动作 → 审计全链路回放（事件顺序不保证，前端按时间排） |
| GET | `/api/ingest/schema` | 可导入的四类数据、字段、别名、质量规则 |
| POST | `/api/ingest/preview` | 上传 CSV/XLSX（multipart）→ 认字段 + 质量报告 + 预览 token（不动库） |
| POST | `/api/ingest/commit` | 按 token 入库：单事务、先整库备份、`ingest_run` 记批次；有行会改动已有数据时返回 409 要求 `confirm_changes` |
| POST | `/api/ingest/undo` | 撤销某一批导入：删掉它新增的行、把改动过的行写回原值 |
| GET | `/api/promo` | 促销日历 + 覆盖范围 + 当前促销天数（登录可见） |
| POST | `/api/promo` | 新增一段促销期（店主），立即回写 `dim_date` |
| PATCH | `/api/promo/{id}` | 改名称/类型/起止（店主），旧日期自动清掉 |
| DELETE | `/api/promo/{id}` | 删一段（店主），同时清掉这段的促销标记 |
| POST | `/api/promo/rebuild` | 手动全量重算促销标记（店主，兜底用） |
| GET | `/api/ingest/runs` | 最近导入批次（文件、行数、新增/变更/无变化、是否已撤销） |
| GET | `/api/ingest/capabilities` | 当前已有数据与缺失指标（缺流量就算不出曝光类根因） |
| POST | `/api/creative/draft` | 按问题码改图（local / dashscope）+ 文案 + 合规检查，产出草稿 |
| GET | `/api/creative/{draft_id}` | 单个创意草稿 |
| GET | `/api/creative?sku_id=&limit=` | 草稿列表 |
| POST | `/api/creative/{draft_id}/apply` | 把草稿转成 `swap_creative` 待审动作（需店长） |

W3 已交付：`/api/actions/*`（dry-run / 起草 / 审批 / 执行 / 回滚）、`/api/audit`、`/api/trace/{id}` + **链路页**（概览 / 时间线 / 动作表，从审批抽屉一键进入）、执行台前端（列表 + 审批抽屉）、**20/30 并发压测**（三套件 P95 全过，[报告](project/docs/load-report.md)）、**容器化文件**（`docker/Dockerfile` + `docker-compose.yml`，本机 daemon 未起故镜像未构建验证）、**促销日历**（`promo_calendar` → `dim_date.is_promo`，导入自动重算）。仍属加分项未做：排练脚本、平台 OAuth 直连（spec §21 设计，文件导入版已交付）。

## 合成数据与注入根因

单店、女装·连衣裙、300 SKU、180 天（2026-05-23 ~ 2026-11-18）、196,213 单。生成器按业务规则下推：曝光 → 点击 → 详情 → 加购 → 支付 → 退款 → 评价，带渠道差异、帕累托热度、周内 / 月度季节性、大促（618 / 99 / 双 11）。

注入 **10 类已知根因**作为 ground truth（`anomaly_gt`），让归因可以被客观打分：

| 代码 | 根因 | 注入信号 |
|---|---|---|
| R1 | 竞品降价战 | cvr ×0.80，竞品价 ×0.88 |
| R2 | 主图素材衰减 | ctr ×0.62 |
| R3 | 热销断货 | 订单 ×0.38 |
| R4 | 尺码信息缺失 | 退款率 ×1.9 |
| R5 | 投放 ROI 恶化 | 花费 ×1.45 |
| R6 | 差评爆发 | cvr ×0.85 |
| R7 | 大促后疲劳 | 曝光 ×0.60（全店） |
| R8 | 竞争加剧 | 曝光 ×0.72 |
| R9 | 价格带错配 | cvr ×0.78，价格 ×1.12 |
| R10 | 物流时效恶化 | 退款率 ×1.5 |

10 类全部可检出（`python tools/sanity_check.py` 输出逐条对比）。

## 实测数字（可直接引用）

| 项 | 数字 |
|---|---|
| 后端测试 | 633 passed（全部离线） |
| 数据 | 300 SKU / 180 天 / 196,213 单 / 24 指标 |
| 归因精度（6 个诊断日 19 张卡） | 加 Critic 前 33% → 加 Critic 后 53%；两轮复审后 77%（13 张卡） |
| 诊断 top-3（70 条评测集） | 0.52（**低于目标 0.80，是当前最大差距**，见[评测报告](project/docs/eval-report.md)） |
| 素材诊断 | mock 10/10（字段 60/60）；真机 VLM 字段 88.3%（detail=low，1.3s/图） |
| 一次问诊端到端 | 冷启动 5.8s / 命中缓存 1.8s |
| 压测 light（20 并发 200 请求） | P50 64ms / P95 103ms / 0% 错误 / RPS 294 |
| 压测 mixed（30 并发 240 请求） | P50 294ms / P95 515ms / 0% 错误 / RPS 90 |
| 压测 heavy `/api/diagnose`（20 并发 40 请求） | **冷缓存 P95 2842ms**（加缓存前 17360ms）/ 热缓存 P95 56ms / 0% 错误 |
| 查数（`/api/query/nl`，规则解析） | 22~82ms |
| 查数（`/api/query/nl`，模型解析 `deepseek-flash`） | 约 4.2~5.5s；同一句话 120s 内重复问 26ms |

## 文档

先看哪份，取决于你要干嘛：

| 想知道 | 看这份 |
|---|---|
| 怎么跑起来、怎么演示、踩过哪些坑 | [运行手册 runbook](project/docs/runbook.md) |
| 这东西怎么拼起来的、为什么这么设计 | [架构说明](project/docs/architecture.md) |
| 52 个接口怎么调、错误码是什么 | [接口说明](project/docs/api.md) |
| 库里有什么表、24 个指标怎么算 | [数据模型与指标口径](project/docs/data-model.md) |
| 要改代码：环境、测试、约定、改动配方 | [开发指南](project/docs/development.md) |
| 评测怎么打分、准确率到底多少 | [评测报告](project/docs/eval-report.md) |
| 压测数据与容器化 | [压测报告](project/docs/load-report.md) |
| 完整设计（28 节，含每条阈值的依据） | [设计说明](project/docs/superpowers/specs/2026-11-18-storedoctor-design.md) |
| 版本演进 | [CHANGELOG](CHANGELOG.md) |

文档索引与术语表在 [project/docs/README.md](project/docs/README.md)；岗位调研与环境适配记录在本地 `reference/`，不随仓库提交。

## 部署

```powershell
cd project
docker compose up --build     # 首次自动生成数据（约 17s）；需要 Docker Desktop 在跑
```

镜像只含后端 API（`python:3.12-slim` + 12 个依赖，非 root），前端用 `npm run build` 出 `dist/` 后交给任意静态服务器；
容器里默认 `EMBED_MODE=off`、`RERANK_MODE=rule`（不装 2.5GB 的本地模型，检索自动降级到 FTS + 规则分）。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| W1 | 数据 / 语义层 / 诊断四件套 / 体检台与诊断页 | ✅ |
| W2 | 策略卡 + 混合检索 + 编排与 SSE + LLM 网关 / 规划器 / 报告改写 + Critic | ✅ |
| W3 | 动作闭环（审批 / 执行 / 回滚 / 审计）+ 执行台 + 链路 + 压测 + 容器化文件 | ✅ |
| 阶段 2 | 素材诊断 + 创意工单 + 数据导入（单商家本地版）+ 促销日历 | ✅ |
| 阶段 2 | 登录鉴权（账号 / 三角色 / Cookie 会话 / 审计取登录人） | ✅ |
| 阶段 2 | 促销日历（商家声明促销期 → 回写 `dim_date`，诊断/R7 跟着变） | ✅ |
| 待办 | 归因 top-3 0.52 → 0.80、排练脚本、50 并发、平台 OAuth | ⏳ |
