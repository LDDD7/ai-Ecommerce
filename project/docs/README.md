# 文档索引

这个目录只放「项目自己的文档」。岗位调研、方案草稿等参考资料在本机 `reference/`，不进仓库。

## 六份文档，各管一件事

| 文档 | 管什么 | 什么时候看 |
|---|---|---|
| [架构说明](architecture.md) | **怎么拼起来的、为什么这么拼**：分层与三条硬边界、模块地图、六条主链路、关键阈值表、13 条设计决策、明确不做的事 | 第一次读代码、评审、要改结构 |
| [接口说明](api.md) | **52 个接口怎么调**：认证与角色、错误格式、按域分组的参数与返回、SSE 事件表、错误码表、curl 示例 | 写前端、写脚本、联调 |
| [数据模型与指标口径](data-model.md) | **库里有什么、指标怎么算**：事实/维/系统表、5 个只读视图、24 个指标公式、维度组、口径约定、合成数据怎么来 | 查数对不上、加指标、接新数据源 |
| [开发指南](development.md) | **要改代码怎么搞**：环境、起服务、测试分层与 fixture 规则、自检工具、六种改动配方、代码约定、踩过的坑、提交清单 | 动手改任何东西之前 |
| [运行手册](runbook.md) | **怎么跑、怎么演示**：配置项、首次登录、生成数据、促销日历、启动、测试、5 步演示脚本、已知坑、实测数字、未做完清单 | 每次启动、演示前 |
| [设计说明](superpowers/specs/2026-11-18-storedoctor-design.md) | **为什么这么设计**：28 节，从范围、数据模型、语义层、归因、策略卡、编排、执行闭环、评测、压测到各专项 | 评审、答辩、改阈值前查依据 |

另外两份专题报告：

| 文档 | 管什么 |
|---|---|
| [评测报告](eval-report.md) | 70 条评测集逐条结果、失败分类、消融、门禁、与目标的差距 |
| [压测与部署报告](load-report.md) | 三套件 × 20/30 并发、冷热缓存、瓶颈与修法、容器化 |

仓库根还有 [README](../../README.md)（这是什么）与 [CHANGELOG](../../CHANGELOG.md)（版本演进）。

## 代码地图

| 目录 | 职责 | 入口 |
|---|---|---|
| `app/data/` | 合成数据：事实表 / 维表 / 系统表，10 类注入根因，300 张素材图 | `python -m app.data.seed` |
| `app/metrics/` | 指标语义层：24 个指标 + 维度组，QuerySpec → 参数化 SQL | `catalog.yaml` / `compiler.py` |
| `app/insights/` | 体检台概览：健康分、异常卡、30 天趋势 | `overview.build_overview` |
| `app/diagnose/` | 归因：异常检测 → 恒等式分解 → 维度贡献 → 漏斗 → 证据链 → 根因假设 → 诊断卡 → Critic → 复审 | `diagnosis.build_diagnosis` |
| `app/playbook/` | 27 张策略卡（YAML）+ 严格 schema + 校验工具 | `loader.load_cards` |
| `app/retrieval/` | FTS5(trigram) + bge-m3 + RRF 混合召回，三级重排 | `hybrid.search` / `rerank.rerank` |
| `app/orchestrator/` | 意图 → 计划（rule-v1 / llm-v1）→ 工具 → 报告（模板 / 模型改写）→ SSE | `runner.run_chat` |
| `app/vision/` | 素材诊断：属性抽取（mock 回放 / VLM）、同行对比、问题折回根因 | `inspector.inspect` |
| `app/creative/` | 创意工单：问题码 → 本地 PIL 改图 / 百炼生图 → 文案 + 合规 → 起草 `swap_creative` | `service.draft` / `service.apply` |
| `app/query/` | 自然语言 → `QuerySpec`（模型出 JSON、规则兜底）；SQL 仍由语义层生成 | `nl.parse_question` |
| `app/ingest/` | 数据接入：canonical 字段 → 表头识别 → 质量闸 → 幂等入库 → 批次撤销 | `service.save_preview` |
| `app/promo/` | 促销日历：商家声明 → `rebuild_flags` 回写 `dim_date.is_promo` | `store.rebuild_flags` |
| `app/actions/` | 执行闭环：五类动作、护栏、风险分级、幂等、状态机、审计、草案生成 | `store.create_action` |
| `app/auth`（`core/`） | 账号、角色、会话、权限守卫 | `core/auth.py` / `core/guards.py` |
| `app/core/` | 配置、运行时设置覆盖、LLM 网关（重试 / 缓存 / 关思考）、TTL 缓存（single-flight） | `llm.get_client` / `cache.get_cache` |
| `app/eval/` | 70 条评测集、跑批、门禁、消融 | `python -m app.eval.runner` |
| `app/api/` | HTTP 层：主接口、动作、创意、鉴权、促销 | `routes.py` 等 6 个文件 |
| `web/` | React 18 + Vite 7 + TS 5.9，八视图：体检台 / 诊断与证据 / 问诊 / 查数 / 数据导入 / 执行台 / 链路 / 设置 | `src/App.tsx` |
| `tools/` | 自检与运维脚本（dev、check_all、load_test、vision_eval、rerender_images…） | `python -m tools.<name>` |
| `docker/` | 后端镜像与入口（库不存在则先生成数据） | `docker compose up --build` |
| `tests/` | **633** 项 pytest，全部离线（`LLM_MODE=mock`） | `pytest` |

## 术语表

| 词 | 含义 |
|---|---|
| **QuerySpec** | Agent 唯一能产出的查询契约（指标 / 维度 / 时间 / 过滤 / 排序 / limit），不能写 SQL；未知字段直接 400 |
| **证据链** | 一条结论必须带 `(指标, 维度值, 时间窗, 变化量, 贡献占比)`，缺证据的卡会被丢掉 |
| **策略卡** | 27 张 YAML，一卡 = 一个根因下的一个动作（`adjust_price` / `adjust_bid` / `swap_creative` / `issue_coupon` / `restock_alert`），带风险等级与预期效果 |
| **Critic** | 反方审查：先规则（方向 / 上游 / 大盘 / 样本 / 促销 / 影响 / 同因），可开关模型版（单卡最多压 20%） |
| **二轮复审 replan** | 同 scope 把窗口扩到 14 天找必需辅证，再升级 / 降级 / 丢弃 / 跳过 |
| **素材诊断** | 把主图读成结构化属性，把「缺尺码表 / 文字占比高」变成证据，折回 R2 / R4 |
| **mock 回放** | 素材图的属性真值存在 `image_feature`，无 key 时直接回放，不调任何视觉模型 |
| **store_state** | 执行闭环写入的「店铺现状」（价格 / 库存 / 出价），执行是 mock，但状态真的会变 |
| **`anomaly_gt`** | 注入根因真值，只给评测用；运行时诊断代码读它就等于作弊 |
| **promo_skew** | 窗口促销日占比 − 基线促销日占比；用于判断「量价是否可比」 |

## 按角色的阅读路径

- **评审 / 面试官**：README → [架构说明](architecture.md) §2 与 §6 → [评测报告](eval-report.md) §6 → spec §22（Critic）
- **接手写代码**：runbook §0–§3 → [开发指南](development.md) → [架构说明](architecture.md) §3 模块地图 → `tests/` 里挑一个模块看契约
- **接数据的人**：[data-model.md](data-model.md) → [api.md](api.md) §7 数据接入 → spec §26
- **现场演示**：runbook「演示脚本」，七步走完闭环
