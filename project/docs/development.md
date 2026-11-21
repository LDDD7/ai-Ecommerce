# 开发指南

> 面向要改代码的人：怎么起、怎么测、约定是什么、常见的坑在哪。
> 只想跑起来看效果，看 [runbook.md](./runbook.md)；想懂结构，看 [architecture.md](./architecture.md)。

## 1. 环境

| 项 | 要求 |
|---|---|
| Python | 3.12（本机 venv：`E:\llm\third\test\k12\.venv`） |
| 依赖 | `pip install -r requirements.txt`（`openpyxl`、`pandas`、`pillow`、`fastapi`、`httpx`…）；向量/重排是可选的 `requirements-embed.txt` |
| Node | 18+（前端）；依赖要自己 `npm install` |
| 数据 | `project/data/store.db` 已随仓库提供（82MB）；要重建见 [data-model.md](./data-model.md) §9 |

```bash
cd project
python -m venv .venv && .venv\Scripts\activate   # 已有 venv 可跳过
pip install -r requirements.txt
```

## 2. 起服务

```bash
# 后端（推荐：先预绑定端口，绕开 Windows 半释放套接字导致的「两个进程抢 8000」）
python -m tools.dev --skip-checks          # 默认 127.0.0.1:8000
python -m tools.dev --reload               # 改代码自动重启
python -m tools.dev --port 8010            # 换端口（跑实验时用，别动演示库）

# 前端（另开一个终端）
cd web
npm install
npm run dev                                # http://127.0.0.1:5173，/api 代理到 8000
```

`tools.dev` 会先做端口预绑定再交给 uvicorn；如果 8000 上有旧进程，它会报错退出——**这是故意的**，比「起了一半、请求打到旧代码」好排查。

生产形态（单进程、静态文件一起发）：

```bash
cd web && npm run build                    # 产出 web/dist
cd .. && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`app/main.py` 检测到 `web/dist` 存在时会挂 `StaticFiles`，直接访问 `http://127.0.0.1:8000` 就是完整页面。

## 3. 目录约定

```
project/
├─ app/                 后端（分层见 architecture.md §3）
├─ tests/               50 个 test_*.py，633 项
├─ tools/               运维/自检脚本（见 §5）
├─ web/                 前端（React + Vite）
│  ├─ src/views/        八视图 + 两个抽屉
│  ├─ src/api*.ts       按域拆的请求层
│  └─ scripts/ui_check.mjs  CSS 类名 ↔ tsx 互查
├─ data/store.db        数据库（+ data/images、data/creative）
├─ docs/                本文档集
├─ samples/             四份导入样例（可直接当演示素材）
└─ docker/              Dockerfile + entrypoint（未构建验证）
```

**一条硬规矩**：后端代码不许 import `tools.*`（`tools` 只依赖 `app`，反过来会把测试/脚本拖进生产路径）。

## 4. 测试

```bash
python -m pytest                           # 全量，约 29s（pytest.ini 已带 -q，末行会打「633 passed」）
python -m pytest tests/test_diagnose.py    # 单文件
python -m pytest -k "critic"               # 关键字
python -m pytest --collect-only -q | tail -3   # 确认收集数（当前 633）
```

> 别再加 `-q`：`pytest.ini` 的 `addopts` 已经有 `-q`，再加一次会变成 `-qq`，**连最后的「N passed」汇总行都不打**，看起来像没跑。

测试分三层：

| 层 | 例子 | 特点 |
|---|---|---|
| 单元 | `test_decompose.py`、`test_critic.py` | 纯函数，不碰库 |
| 集成 | `test_api_diagnose.py`、`test_ingest.py` | 走 ASGI（`httpx` + `TestClient`），用库副本 |
| 端到端 | `test_e2e_flow.py` | 登录 → 诊断 → 起草 → 审批 → 执行 → 回滚 |

三条 fixture 规则（都踩过坑）：

1. `conftest.py` 设 `AUTH_MODE=off`，否则 600+ 用例全 401；
2. 需要真实数据的用例把库复制到 `project/.tmp/<name>/`，**不要用 `tmp_path`**——`%TEMP%` 在这台机器上 ACL 有问题，会 `PermissionError`；
3. 共享 session 级库的用例要先 `DROP TABLE IF EXISTS users/sessions` 并 `auth._FAILS.clear()`，不然密码会串。

## 5. 自检与工具

| 命令 | 用途 |
|---|---|
| `python -m tools.check_all --full` | 7 项离线自检（tests/playbook/vision/compat/data/metrics/diagnosis），约 40~50s，全绿才算能交付 |
| `python -m tools.diagnosis_smoke` | 打印一张诊断卡全文（含 Critic、复审、素材），人眼检查 |
| `python -m tools.metrics_smoke` | 24 指标各跑一次，检查口径 |
| `python -m tools.playbook_lint` | 27 张策略卡结构校验 |
| `python -m tools.vision_eval --mode mock` | 素材评测（mock 10/10；`--mode vlm` 会调模型计费） |
| `python -m app.eval.runner` | 四个评测套件 + 门禁（详细见 [eval-report.md](./eval-report.md)） |
| `python -m tools.load_test --suite heavy` | 压测（详细见 [load-report.md](./load-report.md)） |
| `node web/scripts/ui_check.mjs` | CSS 类名与实际使用互查（漏写样式会在这里暴露） |
| `npx tsc --noEmit`（在 `web/`） | 前端类型检查，必须 0 error |

## 6. 常见改动配方

### 加一个指标

1. `app/metrics/catalog.yaml` 加 `{name, display, source, expr, unit, dims, description}`；
2. 如果 `expr` 用到新列，改 `app/metrics/views.py` 的视图 SQL 与 `VIEW_MEASURES`；
3. 跑 `python -m tools.metrics_smoke`，确认口径；
4. 如果它要参与诊断，改 `app/diagnose/hypothesis.py` 的指标权重与 `anomaly.py` 的方向表；
5. `tests/test_metrics.py` 与 `test_catalog.py` 会兜住「catalog 与视图不一致」。

### 加一类根因（R11…）

1. `hypothesis.py` 的 `ROOT_CAUSES` 加编号与中文名；
2. `anomaly.py` / `diagnosis.py` 加检测规则与证据构造；
3. `critic.py` 的 `EXPECTED_DIRECTION`、`REQUIRED_SUPPORT`、`PROMO_EXEMPT` 补上新代号；
4. `app/playbook/cards/PB-R11-01.yaml` 至少 2 张卡，跑 `playbook_lint`；
5. `app/data/generator.py` 注入一条该根因并写 `anomaly_gt`（否则评测没有真值）；
6. `tools/diagnosis_smoke.py` 看效果，再跑 `app.eval.runner --suite diagnose`。

### 加一张策略卡

按 `PB-R3-02.yaml` 抄结构（`id / title / root_causes / risk / when / metrics / actions[] / expected_effect`），动作的 `type` 必须是 `app/actions/schema.py` 里的五类之一，否则 lint 不过。

### 加一类动作

1. `app/actions/schema.py`：`ACTION_SPECS` 加类型、参数 schema、风险等级、`expected_effect`；
2. `app/actions/execute.py`：`_apply_*` 写 `store_state`，并返回 diff；
3. `app/actions/plan.py`：起草模板；
4. `tests/test_actions.py`：dry-run、执行、幂等、回滚各一条；
5. 前端 `views/ActionDetail.tsx` 的 `PARAM_LABEL` 补中文，`audit-labels.ts` 的 `TYPE_LABEL` 补名称。

### 加接口

1. 写路由（记得 `Depends(require_role(...))`，写接口至少要 manager）；
2. 错误统一 `{"error": code, "message": ...}`，新码补进 `docs/api.md` §10；
3. 前端在对应 `api-*.ts` 加函数，别在组件里裸 `fetch`；
4. `tests/test_api_*.py` 补一条 200 与一条错误码。

## 7. 代码约定

- **中文注释写「为什么」**，不写「做了什么」（代码本身能看出做了什么）；
- 所有 pydantic DTO 一律 `ConfigDict(extra="forbid")`——宁可 400，也不要静默丢字段；
- 金额 `round(x, 2)`，比率 4 位小数，时间一律 `YYYY-MM-DD` 字符串（SQLite 里就是这么存的）；
- 结构化输出进 `meta`（耗时、降级原因、命中的引擎），别只打日志——演示时要能解释「为什么这么慢」；
- 失败要可降级：模型挂了回规则、视觉挂了回 mock、促销重算挂了只记原因，**不能把主流程拖死**；
- 前端文案不许出现英文标识、JSON、`t9` 这类内部代号（统一走 `web/src/audit-labels.ts`）。

## 8. 踩过的坑（别再踩）

| 坑 | 症状 | 正确做法 |
|---|---|---|
| PowerShell 读写 UTF-8 中文 | 整份 README 变 `鍚庣娴嬭瘯`，二次写后 U+FFFD 不可逆 | 中文文件只用 `read`/`write`/`edit` 或 Python 读写；**永远别用 `Get-Content`+`Set-Content` 改中文** |
| `%TEMP%` ACL | pytest `tmp_path` 报 `PermissionError`，SQLite 建不了 `-wal` | 临时文件放 `project/.tmp/` |
| `data/` 目录继承 DENY ACE | 新建 `-wal` 失败 | 迁移/实验用「复制到 `.tmp` → 改成 DELETE journal → 原位 `r+b` 覆盖」 |
| `tools` 包名冲突 | `ModuleNotFoundError: tools.vision_eval` | `project/tools/__init__.py` 已存在（挡住 site-packages 的同名正规包），别删 |
| 端口半释放 | 旧进程还在、新进程也能绑上 8000，请求打到旧代码 | 用 `tools.dev`（预绑定）；改完杀进程并用新字段确认 |
| FTS5 trigram 的 `LIKE` | 短词（`尺码`）查不到 | 兜底改成取出后 Python 子串匹配，不要用 SQL `LIKE` |
| 模型开思考 | 8.7s、`max_tokens` 被思考吃掉、正文 0 字 | `LLM_THINKING=off`（默认），换网关不认该参数会自动退让 |
| 前端起不来 | `Error: spawn EPERM` | 沙箱不允许 esbuild 子进程，只能在本机跑 `npm run dev` |

## 9. 提交前清单

```bash
python -m pytest                          # 633 passed
python -m tools.check_all --full          # 7/7 PASS
cd web && npx tsc --noEmit && node scripts/ui_check.mjs && cd ..
git status                                # 确认没有 .tmp/、data/*.db-wal、__pycache__
```

> 注：当前工作区**没有 `.git`**（`trash/20261120_.git` 是旧元数据备份）。要纳入版本控制需要自己 `git init`，`.gitignore` 已经写好（`.tmp/`、`data/*.db*`、`web/node_modules`、`web/dist`）。
