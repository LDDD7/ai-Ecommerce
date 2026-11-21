# 运行手册

> 目标：从零到「页面上看见诊断结果」，命令都能直接粘。所有测试与评测默认**不调用外部模型**，只走 mock 与本地文件。

## 0. 环境

| 项 | 本机值 | 说明 |
|---|---|---|
| Python | `E:\llm\third\test\k12\.venv\Scripts\python.exe`（3.12.10） | 已装 fastapi / pydantic / openai / pillow 等，**不需要再装后端依赖** |
| Node | v24.19.0 / npm 11.18.0 | 前端依赖需自己 `npm install`（沙箱装不了） |
| 数据库 | SQLite 3.49.1（单文件） | 不需要 Docker / Postgres / Redis |
| 向量与重排 | 本地 `bge-m3`（1024 维）、`bge-reranker-v2-m3` | 路径在 `.env` 里；`RERANK_MODE=rule` 时根本不加载 reranker |

## 1. 配置（`project/.env`）

从 `.env.example` 复制，按需改。关键变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` | `https://api.deepseek.com/v1` / 空 | OpenAI 兼容端点；**留空时全链路自动走规则版与 mock 回放**，功能不缺 |
| `LLM_MODEL_PLANNER` / `_FAST` / `_VISION` | `deepseek-flash` | 规划器 / 报告改写 / 素材检查，三档可分 |
| `LLM_MODE` | `auto` | `auto` 有 key 走真、无 key 走规则；`real` 强制真调；`mock` 强制规则（测试用） |
| `LLM_THINKING` | `off` | **必须关**：开思考时同一条规划 8.7s/644 tokens，关掉 2.2s/279 tokens |
| `LLM_TIMEOUT_MS` / `LLM_RETRIES` | `12000` / `1` | 端到端预算 8s，超时宁可降级 |
| `REPORT_BUDGET_MS` | `7000` | 工具跑完已花超过这个时间，就跳过模型改写用模板 |
| `CRITIC_LLM` | `false` | 打开后 Critic 会额外让模型挑错，整体会到 7s 以上 |
| `VISION_MODE` | `auto` | `mock` 永远回放合成图真值；`vlm` 强制现算 |
| `DEMO_AS_OF` | `2026-11-17` | 演示基准日（双 11 后透支窗口 + 热销断货，5 张卡、命中 4/5） |
| `EMBED_MODE` / `EMBED_MODEL_PATH` | `local` / 空 | 空则跳过向量召回，只走 FTS |
| `RERANK_MODE` | `rule` | `off` / `rule` / `cross` / `llm` |
| `DB_PATH` | `data/store.db` | 相对 `project/` |
| `AUTH_MODE` | `password` | `password` 需要登录；`off` 关掉鉴权（仅本机调试/自动化测试） |
| `AUTH_ADMIN_PASSWORD` | 空 | 首次启动建 `admin` 的初始密码；留空则随机生成到 `.tmp/first-run.txt` |
| `SESSION_TTL_HOURS` | `168` | 登录态有效期（小时） |
| `COOKIE_SECURE` | `false` | 上 HTTPS 时改 `true` |

> 想完全离线演示：把 `LLM_API_KEY` 留空、`VISION_MODE=mock`，其它不用动。

## 1.5 首次登录

后端起来后第一次打开前端（或 `curl http://127.0.0.1:8000/api/health` 之外任何 `/api/*`）会要求登录：

1. 用户名 `admin`；
2. 初始密码在 `project/.tmp/first-run.txt`（`log` 里也会打印一次），或用 `AUTH_ADMIN_PASSWORD` 事先指定；
3. 第一次登录会被要求改密（`must_change`），改完才能进主界面；
4. 之后在「设置 → 账号与权限」里建运营/店长账号，角色决定能看到哪些入口、能不能审批。
5. 接着去「设置 → 促销日历」把商家自己的促销期填上（比如「双 11 · 10-20 ~ 11-11」）。
   不填的话，导进来的促销日会被当成普通的一天，R7（大促透支）永远识别不出来。

> 忘了密码：停掉后端，用
> `python -c "from app.core import auth; auth.update_user(r'data/store.db', 'u_xxx', password='新密码')"`
> 不熟就直接删掉 `users` 表里那一行再重启（会重新 bootstrap 一次 `admin`）。
> 本机单点调试想省掉登录：`.env` 里设 `AUTH_MODE=off` 并重启（接口全部放行，别在有别人的机器上用）。

## 2. 生成数据（已生成过，可跳过）

```powershell
cd E:\llm\third\Ecommerce\project
& 'E:\llm\third\test\k12\.venv\Scripts\python.exe' -m app.data.seed            # 全量：300 SKU / 180 天 / 19.6 万单，约 17s，产出 data/store.db 78MB + 300 张主图
& 'E:\llm\third\test\k12\.venv\Scripts\python.exe' -m app.data.seed --tiny     # 自检：20 SKU / 30 天 / 3000 单
```

## 2.5 促销日历（新商家第一件事）

导入的订单本身不带「这天是不是促销」的信息，需要在「设置 → 促销日历」里声明：

1. 名称随意（`双 11` / `店庆` / `黑五`），类型选平台大促或店铺活动；
2. 起止日期按真实活动填，单段最多 90 天（长促拆成几段）；
3. 保存即生效：会重算这一段日期的 `dim_date.is_promo`，诊断、Critic、查数维度同时跟上；
4. 改期或删除都会清掉旧日期，不会「越改越多」。

> 演示库已内置三段（618 / 99 划算节 / 双 11，共 50 天），不需要再手动加。

## 3. 启动

```powershell
cd E:\llm\third\Ecommerce\project
python -m tools.dev                # 起后端 8000，并打印前端启动命令
python -m tools.dev --port 8010    # 换端口
python -m tools.dev --reload       # 开发热重载
```

> 也可以用 `.\tools\dev.ps1`，但若本机执行策略是 Restricted/AllSigned 会被拦（报 `UnauthorizedAccess`），
> 那就改用 `python -m tools.dev`，或 `powershell -ExecutionPolicy Bypass -File .\tools\dev.ps1`。

前端另开一个终端（**必须你自己跑**，沙箱起不了 esbuild 子进程）：

```powershell
cd E:\llm\third\Ecommerce\project\web
npm run dev                        # http://localhost:5173 ，/api 已代理到 8000
```

自检三连：

```powershell
curl.exe -s http://127.0.0.1:8000/api/health
curl.exe -s "http://127.0.0.1:8000/api/overview?days=7"
curl.exe -s http://127.0.0.1:8000/api/diagnose | more
```

## 4. 测试与评测

| 命令 | 做什么 | 耗时 | 是否耗额度 |
|---|---|---|---|
| `pytest` | **559** 项单测 + 接口测试（`LLM_MODE=mock`） | ~17s | 否 |
| `python -m tools.playbook_lint` | 27 张策略卡校验：根因覆盖 / risk / 动作分布 / 指标名 | <1s | 否 |
| `python -m tools.vision_eval --mode mock --cases 10` | 素材诊断 10 条，整条 10/10、字段 60/60 | <1s | 否 |
| `python tools/sanity_check.py` | 数据自检：行数、大盘、10 类注入根因逐条对比 | ~3s | 否 |
| `python -m tools.compat_check` | 环境适配（SQLite / FastAPI / 流式 / yaml …） | ~5s | 否 |
| `python -m tools.metrics_smoke` | 语义层手工冒烟（查询示例） | ~2s | 否 |
| `python -m tools.diagnosis_smoke 2026-11-17 5` | 打印诊断卡全文（含 Critic 结论） | ~2s | 否 |
| `python -m tools.index_vectors` | 重建 37 条语料的 FTS + 向量索引（需要 bge-m3） | ~30s | 否 |
| `python -m tools.vision_eval --mode vlm --cases 10` | 真调视觉模型对 10 张图打分 | ~13s | **是**（10 次视觉调用） |
| `python -m tools.rerender_images` | 按属性重画 300 张素材图并回写真值 | ~20s | 否 |
| `python -m tools.load_test --suite heavy --concurrency 20 --requests 40` | 压测最重接口（冷缓存 P95 2.8s / 热缓存 56ms） | ~3s | 否 |
| `python -m tools.load_test --suite mixed --concurrency 30 --requests 240` | 混流压测（P95 515ms，0 错误） | ~3s | 否 |

## 5. 演示脚本（5 步，约 3 分钟）

1. **体检台**：打开 5173 → 首屏是「诊断书」——健康分 49 / D、成交额同比 -53.2%、五张卡；红笔圈出的那个百分比是本周最要紧的一处。
2. **诊断与证据**：点「诊断与证据」→ 每张卡：根因、影响金额与占降幅、置信度、证据表（指标 / 基线 / 当期 / 变化 / z）、反方意见（Critic）、素材块（所有 SKU 级卡都有，点「生成改进素材」打开面板）。
3. **问诊**：点「问诊」→ 输入「11-17 那周成交额为什么掉了？甜美款是不是有问题」→ 看 plan 事件、4 个工具调用、逐卡 evidence、playbook 建议动作、报告逐字流式输出；页脚告诉你用的是 `llm-v1` 还是 `rule-v1`、报告是模型改写还是模板。
4. **模型反驳自己**（可选，需 key）：`/api/diagnose?critic=llm` 或前端带参访问，展示「模型能提出反方意见，但不能自己改分数」。
5. **离线兜底**：把 `.env` 的 `LLM_API_KEY` 清空重启，同样五步全部可用（规则版 + mock 回放），证明不依赖外部服务。
6. **数据导入**（可选）：点「数据导入」→ 选 `project/samples/taobao_orders.csv` → 看映射与质量报告 → 确认入库；同一个文件再导一次是「无变化 15 行」；把某个订单的数量改成 2 再导，会先弹「1 行会覆盖已有数据」的差异面板，确认后入库、点「撤销」可退回原值。
7. **Trace**（可选）：执行台打开任一动作 → 抽屉里「链路」那一行点「查看 trace」→ 跳到 Trace 页看这条 `trace_id` 下的全部动作与审计事件（概览条 + 时间线 + 动作表）；也可以直接在 Trace 页的输入框粘 `tr_xxx`，或点「最近」里的 chip。页尾折叠的「技术细节」才是原始 JSON，默认界面全是中文。

## 6. 已知坑（踩过的，别再踩）

| 现象 | 原因 | 处理 |
|---|---|---|
| `npm run dev` / `npm run build` 报 `Error: spawn EPERM` | 沙箱禁止 esbuild 起子进程 | 前端由本机终端跑；我只能 `tsc --noEmit` |
| 控制台中文乱码 / `UnicodeEncodeError` | PowerShell 默认 GBK | 跑 Python 前 `$env:PYTHONIOENCODING='utf-8'`；脚本内 `sys.stdout.reconfigure(encoding='utf-8')` |
| pytest 报 `WinError 5` / `unable to open database file` | `%TEMP%` 子目录 ACL 异常 | 测试产物一律放工作区 `.tmp/`（`pytest.ini` 已 `-p no:cacheprovider`） |
| 改中文文件后变乱码 | 用 `Get-Content/Set-Content` 管道读写 UTF-8 | **只用编辑工具改中文文件**，别走 PowerShell 管道 |
| 检索「断货」只搜到少量/为空 | trigram 分词器要求 ≥3 字连续片段；短词只能走子串兜底 | 已在 `app/retrieval/fts.py` 用 Python 子串兜底（按命中次数排序），2 字词也有结果；向量召回仍是长句的主召回 |
| `pytest` 报 `ModuleNotFoundError: No module named 'tools.vision_eval'` | 环境里装过同名 `tools` 包（`site-packages/tools`），正规包压过命名空间包 | 已加 `project/tools/__init__.py`，本目录的 `tools` 优先 |
| 首次 `RERANK_MODE=cross` 很慢 | bge-reranker 冷启动 18.8s，超 3.5s 预算 | 首次调用降级到规则版，第二次起正常；默认保持 `rule` |
| 端到端偶发 7s+ | 模型网络抖动 + 重试 | 有 `REPORT_BUDGET_MS` 闸；命中 `llm_cache` 后 1.8s |
| 诊断日看起来「数据坏了」 | 演示日落在 R7 大促透支窗口（成交额 -53% 是真的） | 这是注入的真根因，不是 bug |
| `python -m tools.dev` 报 `[Errno 10048]` / 端口被占但 `netstat` 看不到 | Windows 上有半释放的套接字占着 8000，uvicorn 默认不带 `SO_REUSEADDR` | `tools.dev` 现在自己预绑定 socket（`SO_REUSEADDR`）再交给 uvicorn；也可 `--port 8010` |

## 7. 当前实测数字（可直接引用，不必重跑）

| 指标 | 值 |
|---|---|
| 后端测试 | **633 passed** |
| 归因总体精度（6 个候选日，卡 vs `anomaly_gt`） | 33% → 53%（Critic）→ 77%（第二轮复审）→ **79%**；top-3 命中 5/6；召回不丢 |
| 演示日（2026-11-17） | **4 张卡，命中 4/4**；健康分 49 / D |
| 素材属性抽取 | mock 回放 10/10、字段 60/60；真 VLM 字段 **88.3%**（53/60）、1.3s/图 |
| 一次问诊端到端 | 冷启动 **5.8s**、命中缓存 **1.8s**；带执行闭环（5 工具 + 起草）**2.5s**（mock planner） |
| 单次诊断（HTTP，规则版） | 978ms（不含复审）→ **1.7~1.9s**（含 14 天复检） |
| 执行闭环 | 五类动作、12 个端点；起草 → 审批 → 执行 → 回滚 → 审计全链路跑通；审计与 Trace 页全部中文（无 JSON） |
| Trace 页 | `tr_t9http`：4 个动作 / 9 条事件；从审批抽屉「查看 trace」一键进入 |
| 检索短词 | 「尺码」「断货」等 2 字查询命中 fts#1（原先 trigram 下恒为 0 行，已修） |
| 创意工单 | 本地改图 322ms：文字占比 38.6% → 15.3% 并补上尺码表；合规通过 → 起草换素材动作 → 执行 → 回滚全通 |
| 查数 | 结构化 `POST /api/query` 30~80ms；自然语言 `POST /api/query/nl` 规则解析 22~82ms、模型解析 4.2~5.5s（同句 120s 内缓存 26ms） |
| 数据规模 | 300 SKU / 180 天 / 196,213 单 / GMV ¥36.95M / 300 张 800×800 主图 |

## 8. 还没做完的

- 排练脚本（把「体检台 → 诊断 → 问诊 → 查数 → 执行 → 创意工单」串成一遍 5 分钟演示）
- 平台 OAuth 直连 / 定时同步（文件导入版已交付：`/api/ingest/*`，含提交前差异确认与批次撤销）
- 促销日历接入（导入产生的新日期 `dim_date.is_promo` 恒 0，R7 促销偏斜判定对新商家库偏保守）
- Docker 镜像构建验证（文件齐备，本机 daemon 未起）
- 百炼真机出图（适配器按文档实现 + MockTransport 测试，未用真实额度验证）
