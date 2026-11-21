# 压测与部署报告（T10）

> 目的：验证 spec §3.8 的「P95 ≤8s、错误率 <1%」在**真实并发**下成立，并给出容器化方案。
> 所有数字都是本机实测（Windows 11 / Python 3.12.10 / SQLite 3.49.1 / 单进程 uvicorn），不是估算。

## 1. 怎么压

```bash
cd project

# 起后端（自带端口预绑定，绕开 Windows 的半释放套接字）
python -m tools.dev --skip-checks

# 另一终端
python -m tools.load_test --suite heavy --concurrency 20 --requests 40
python -m tools.load_test --suite mixed --concurrency 30 --requests 240 --json --out .tmp/mixed30.json
```

| 套件 | 打哪些接口 | P95 线 | 说明 |
|---|---|---|---|
| `light` | `/api/health`、`/api/query` | 3000ms | 轻接口基线 |
| `mixed` | health / overview / query / playbook.search | 3000ms | 真实混流 |
| `heavy` | `/api/diagnose?as_of=2026-11-17` | 8000ms | 最重的一条，故意打满 |

- `--warmup 0` 关预热（测冷缓存）；`--json` 出机器可读结果；退出码 1 = 越线。
- 并发取 **20 / 30**（用户要求「二三十就够」），不追 50。

## 2. 首轮：没有缓存时（问题暴露）

| 套件 | 并发 / 请求 | P50 | P95 | RPS | 错误率 | 结论 |
|---|---|---|---|---|---|---|
| light | 20 / 200 | 61ms | 205ms | 246.8 | 0% | 通过 |
| mixed | 20 / 200 | 195ms | 2297ms | 25.5 | 0% | 勉强通过（`/api/overview` 均值 1954ms） |
| heavy | 20 / 40 | 14617ms | 17360ms | 1.3 | 0% | **不通过** |

**错误率是 0，但重接口线性堆积**：`/api/diagnose` 单发 1.5~2.5s，20 个并发同时打就是 17s。
原因不神秘——FastAPI 同步路由跑在线程池里，每个请求都在 SQLite 上把同一份结果重算一遍。

## 3. 修法：TTL 缓存 + single-flight

`project/app/core/cache.py`：

- `TTLCache(ttl_s=120, maxsize=64)`：进程内 LRU + TTL。
- 缓存键 = `(db_path, round(db_mtime, 3), *业务参数)`——数据重新生成后 mtime 变化，缓存**自动失效**，
  不依赖手动清。
- `get_or_compute()` 做 **single-flight**：同键并发只有一个线程真算，其余挂在键锁上等，算完一起取结果。
  没有这一步，20 个同时到达的请求会各自 miss 一次，缓存对尖峰完全无效（这是第二次实测才发现的问题）。
- `/api/overview`、`/api/diagnose` 都走它，响应体加 `meta.cached`，方便肉眼确认命中。

**踩坑记录**：第一次加完缓存，heavy 还是 17s，`meta.cached` 字段干脆不存在。
排查后确认是**旧 uvicorn 进程仍占着 8000**——`tools.dev` 用 `SO_REUSEADDR` 预绑定，
Windows 上两个进程能同时绑同一个端口，请求随机落到旧进程（旧代码没有缓存）。
判断「跑的是不是新代码」最省事的办法：看响应里有没有新字段。

## 4. 修后结果（同一台机器，`data/store.db` 82MB，196,213 单）

| 套件 | 并发 / 请求 | P50 | P95 | P99 | RPS | 错误率 |
|---|---|---|---|---|---|---|
| light | 20 / 200 | 64ms | 103ms | 122ms | 294.4 | 0% |
| mixed | 20 / 200 | 152ms | 538ms | 585ms | 92.0 | 0% |
| mixed | 30 / 240 | 294ms | 515ms | 598ms | 90.0 | 0% |
| heavy（**冷**缓存，`--warmup 0`） | 20 / 40 | 2810ms | **2842ms** | 2842ms | 13.7 | 0% |
| heavy（热缓存） | 20 / 40 | 31ms | 56ms | 58ms | 526.9 | 0% |
| heavy（热缓存） | 30 / 60 | 51ms | 71ms | 73ms | 492.1 | 0% |

- 冷缓存 P95：**17360ms → 2842ms**（单飞把 20 并发收敛成「1 次计算 + 19 次等待」）。
- 热缓存 P95：**56ms**，比单发还快——直接读内存。
- 6 组配置**全部通过**，错误率 0%。

## 5. 诚实边界

1. **单进程**。缓存是进程内字典，多 worker 各存一份（不会错，命中率下降）。
   真要横向扩，第一步是把缓存换 Redis，而不是加 worker。
2. **SQLite 写锁**。读能并发，写（`/api/actions/*`）靠 `BEGIN IMMEDIATE` 串行；单店演示够用，多商户不行。
3. **缓存键不含租户**。目前单库单店；接多店时要加 `store_id`（见 spec §21 多租户留白）。
4. **冷启动 2.8s 是「一个请求扛 20 个并发」**。如果 20 个并发打的是 20 个**不同** `as_of`，
   P95 会回到 15s 量级——每个时间窗都得真算一次，这是设计约束不是 bug。
   演示时把「切换日期」做成低频操作（当前 UI 就是这么做的）。

## 6. 容器化

| 文件 | 作用 |
|---|---|
| `project/requirements.txt` | 运行时依赖（12 个包，钉版本） |
| `project/requirements-embed.txt` | 可选：本地向量/重排（torch 一系，2.5GB，不装则自动降级） |
| `project/docker/Dockerfile` | python:3.12-slim，非 root（uid 10001），`HEALTHCHECK` 打 `/api/health` |
| `project/docker/entrypoint.py` | 库不存在就 `app.data.seed` + `tools.index_vectors`，再起 uvicorn |
| `project/docker-compose.yml` | 挂载 `./data`，`EMBED_MODE=off`/`RERANK_MODE=rule`（容器里没装模型） |
| `project/.dockerignore` | 排除 data/、web/node_modules、.tmp、tests、docs |

```bash
cd project
docker compose up --build     # 首次会自动生成数据（约 17s），之后直接起服务
curl http://127.0.0.1:8000/api/health
```

**状态**：文件齐全、逻辑可解释，但**本机 Docker daemon 未启动，镜像未构建验证**。
需要验证时先开 Docker Desktop，再跑上面那条命令。

## 7. 复现清单

```bash
python -m pytest -o addopts='' -q        # 495 passed
python -m tools.check_all --full         # 7 项离线自检
python -m tools.load_test --suite heavy --concurrency 20 --requests 40 --warmup 0
```
