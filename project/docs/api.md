# 接口说明

> 共 **52 个接口**，按域分组。所有接口都在 `/api` 前缀下（`/api/health`、`/api/diagnose` …）。
> 本文只写「怎么调、返回什么、什么情况会报错」；字段为什么这么设计看 [architecture.md](./architecture.md) 与 [data-model.md](./data-model.md)。

## 0. 约定

| 项 | 约定 |
|---|---|
| 基地址 | 本机 `http://127.0.0.1:8000`；前端 dev 用 Vite 代理 `/api` |
| 认证 | 登录后下发 Cookie `sd_session`（HttpOnly、`SameSite=Lax`），后续请求浏览器自动带；脚本用 `-c/-b cookie.txt`。`AUTH_MODE=off` 时全部放行 |
| 角色 | `staff`（运营）< `manager`（店长）< `owner`（店主）；写接口在表里标了最低角色 |
| 请求体 | JSON（导入预览是 `multipart/form-data`） |
| 错误格式 | `{"error": "<code>", "message": "...", "detail": [...]}`；HTTP 状态码：400 参数错、401 未登录、403 权限不足、404 不存在、409 需要确认/状态冲突、503 依赖不可用 |
| 金额 | 单位元，两位小数；比率是 0~1 的小数（`0.0452` = 4.52%） |
| 时间 | `YYYY-MM-DD`；不传 `as_of` 用配置里的演示基准日（默认 `2026-11-17`） |

## 1. 认证与账号（8）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | `{username, password}` → 下发 Cookie，返回用户（含 `must_change`） |
| POST | `/api/auth/logout` | 登录 | 删会话，清 Cookie |
| GET | `/api/auth/me` | 公开 | 当前用户；未登录返回 401（前端用它判断是否要显示登录页） |
| POST | `/api/auth/password` | 登录 | `{old_password, new_password}`；改完踢掉其它会话 |
| GET | `/api/auth/users` | owner | 账号列表（不含任何密码信息） |
| POST | `/api/auth/users` | owner | `{username, display_name, password, role}` |
| PATCH | `/api/auth/users/{user_id}` | owner | `{role?, disabled?, password?, display_name?}`，至少一项 |
| GET | `/api/auth/whoami` | 公开 | 调试用：返回是否启用鉴权、用户名 |

```bash
curl -c cookie.txt -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"StoreDoctor2026"}'

curl -b cookie.txt http://127.0.0.1:8000/api/auth/me
```

防爆破：同一用户名 5 次失败锁 5 分钟（内存计数，重启清零）。

## 2. 只读查询（8）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 库大小、订单数、最后日期、指标数、LLM 状态、planner 名称 |
| GET | `/api/metrics/catalog` | 24 个指标：名称、中文标签、单位、可用维度、同义词 |
| GET | `/api/overview?days=7&as_of=` | 健康分（0~100 + 等级）、告警、6 个核心 KPI、环比 |
| POST | `/api/query` | `{spec: QuerySpec}` → 行数据 + 解析后的 SQL 摘要 |
| POST | `/api/query/nl` | `{question, as_of?, window_days?, use_llm?}` → 解析出的 spec + 直接跑结果 |
| POST | `/api/playbook/search` | `{query?, root_causes?, metrics?, risk_max?, limit?, mode?}` → 策略卡 |
| GET | `/api/images/{sku_id}?role=main` | 主图 PNG；缺图 404 `image_missing`，role 非法 400 `invalid_role` |
| GET | `/api/llm/probe` | 真发一次「只回四个字」，返回耗时与 token；`mock` 模式返回 `ok:false` |

### QuerySpec

```json
{
  "metrics": ["gmv", "paid_orders"],
  "dimensions": ["dt"],
  "time": {"start": "2026-11-11", "end": "2026-11-17"},
  "filters": [{"field": "style", "op": "=", "value": "甜美"}],
  "order_by": [{"field": "gmv", "dir": "desc"}],
  "limit": 100
}
```

- 上限：`metrics ≤ 6`、`dimensions ≤ 3`、`filters ≤ 8`、`order_by ≤ 2`、`limit ≤ 5000`、时间跨度 ≤ 400 天；
- **未知字段是硬错误**：多传一个 `dims` 会得到
  `400 {"error":"invalid_spec","detail":[{"loc":"body.spec.dims","msg":"未知字段「dims」；只接受 metrics / dimensions / time / filters / order_by / limit"}]}`；
- 只能查 24 个指标和 5 个视图（`traffic/orders/ads/reviews/inventory`）能提供的维度与度量，别的字段一律 400，不拼 SQL。

## 3. 诊断（3）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/diagnose` | 见下 |
| GET | `/api/diagnose?as_of=&window_days=7&max_cards=5&critic=rules&material=true&replan=true` | 同上，查询参数版（方便浏览器直开） |
| POST | `/api/vision/inspect` | `{sku_id, role, mode?}` → 素材属性 + 问题码 + 折算根因/建议 |

`/api/diagnose` 入参（全部可选）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `as_of` | 配置演示日 | 诊断基准日（含当天） |
| `window_days` | 7 | 诊断窗口 |
| `max_cards` | 5 | 最多返回几张卡 |
| `critic` | `rules` | `rules` 规则版反方审查（~0.6s）；`llm` 再让模型反驳一遍（+2s，只降不升） |
| `material` | false | 给 SKU 级卡补素材块（读 `image_feature`，不发视觉请求） |
| `replan` | true | 二轮复审：同 scope 扩到 14 天找必需辅证（+0.85s） |

返回骨架：

```json
{
  "as_of": "2026-11-17",
  "window": {"start": "2026-11-11", "end": "2026-11-17", "days": 7},
  "decomposition": {"gmv_now": 1014014.07, "delta_pct": -0.532, "expanded": [...]},
  "funnel": {"steps": [...]},
  "cards": [
    {
      "card_id": "card_001", "code": "R7", "title": "大促后透支",
      "scope": {"dim": "style", "value": "度假"},
      "confidence": 0.57, "severity": "high",
      "gmv_impact": -263681.0, "impact_share": 0.23,
      "narrative": "…", "evidence": [{"metric": "paid_orders", "baseline": 276.7, "current": 149.4, "delta_pct": -0.46, "z": -4.2}],
      "caveats": ["窗口内促销日占比变化 -0.57，量价对比需谨慎", "反方：…"],
      "critic": {"verdict": "weaken", "confidence_before": 0.64, "factor": 0.89, "objections": ["…"]},
      "material": {"score": 0.75, "findings": [...], "evidence": [...], "compare_summary": "…"}
    }
  ],
  "meta": {
    "cards_before_critic": 6,
    "critic": {"engine": "rules-v1", "checked": 5, "support": 3, "weaken": 2, "reject": 0, "elapsed_ms": 320},
    "replan": {"upgraded": 1, "demoted": 1, "dropped": 0, "elapsed_ms": 850},
    "material": {"requested": true, "attached": 2, "targets": 2, "degraded": false},
    "elapsed_ms": 2111
  }
}
```

被 Critic 驳回的卡不会出现在 `cards` 里，但记录在 `meta.critic.rejected` —— 结论可追溯。

## 4. 问诊 / SSE（1）

`POST /api/chat`，`{message, as_of?, window_days?}`，返回 `text/event-stream`。每个事件一行 `data: {...}`：

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `plan` | `steps[{tool,args,why}]`、`planner_trace` | 先给用户看「准备怎么查」 |
| `tool_call` | `tool`、`args` | 开始调工具 |
| `tool_result` | `tool`、`ms`、`brief` | 工具完成与摘要 |
| `diagnosis` | `cards`、`meta` | 诊断卡（前端就地渲染） |
| `evidence` | 每条证据 | 证据表逐行推 |
| `critic` | `verdicts` | 反方意见 |
| `playbook` | `actions[{card_id,title,risk,expected_effect}]` | 建议动作 |
| `action_draft` | `type`、`params`、`risk_level` | 起草的待审动作 |
| `token` | `text` | 报告正文增量 |
| `done` | `elapsed_ms`、`planner`、`report_source`、`report_reason` | 收尾；`report_source` 是 `llm` 或 `template` |

```bash
curl -N -b cookie.txt -X POST http://127.0.0.1:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"message":"最近为什么掉得这么厉害","as_of":"2026-11-17"}'
```

## 5. 动作闭环（12）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/actions/types` | 登录 | 五类动作的定义、参数、风险等级、可执行条件 |
| POST | `/api/actions/dry-run` | 登录 | 只校验不改库：`{type, params, sku_id?}` |
| POST | `/api/actions` | 登录 | 直接建动作（跳过 LLM 起草） |
| POST | `/api/actions/draft` | 登录 | 按诊断上下文起草（`{sku_id, code, ...}`），body 是自由 JSON |
| GET | `/api/actions?status=&type=&limit=50&offset=0` | 登录 | 列表 + `status_counts` + `total` |
| GET | `/api/actions/{action_id}` | 登录 | 单个动作（含审计时间线） |
| POST | `/api/actions/{action_id}/approve` | manager | `{approver?, role?, note?}`；risk 2 需 owner |
| POST | `/api/actions/{action_id}/reject` | manager | `{actor?, reason}` |
| POST | `/api/actions/{action_id}/execute` | manager | mock 执行，写 `store_state` 与 diff |
| POST | `/api/actions/{action_id}/rollback` | manager | 按审计里的 revert 写回 |
| GET | `/api/audit?action_id=&limit=50` | 登录 | 审计事件（`drafted/approved/rejected/executed/rolled_back`） |
| GET | `/api/trace/{trace_id}` | 登录 | 一条链路：`{trace_id, actions[], events[], count}`；事件顺序不保证，按 `created_at` 排 |

操作人（`actor/approver`）优先取登录用户，未登录（`AUTH_MODE=off`）才用请求体里的名字。

## 6. 创意工单（5）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/creative/draft` | manager | `{sku_id, issues[], target, provider?, constraints?, note?}` |
| GET | `/api/creative?sku_id=&limit=20` | 登录 | 草稿列表 |
| GET | `/api/creative/{draft_id}` | 登录 | 单个草稿（图 + 文案 + 合规 + 改法） |
| GET | `/api/creative/asset/{draft_id}` | 登录 | 出图 PNG |
| POST | `/api/creative/{draft_id}/apply` | manager | 把草稿转成一条 `swap_creative` 待审动作 |

`provider`：`local`（PIL 本地改图，默认）/ `dashscope`（百炼生图，要 key）/ 不传按设置页。合规 `fail` 时不能起草。

## 7. 数据接入（6）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/ingest/schema` | 登录 | 四类数据（orders/traffic/products/refunds）的字段、别名、质量规则 |
| POST | `/api/ingest/preview` | owner | `multipart`：`file`、`dataset?`、`mapping?`（JSON 串）→ 识别 + 质量报告 + token |
| POST | `/api/ingest/commit` | owner | `{token, allow_warn?, confirm_changes?}`；有行会改已有数据时 **409** 要确认 |
| POST | `/api/ingest/undo` | owner | `{run_id}` 撤销这一批（删新增行、还原改动行） |
| GET | `/api/ingest/runs?limit=20` | 登录 | 批次记录：新增/变更/无变化、状态 |
| GET | `/api/ingest/capabilities` | 登录 | 已有数据与缺失指标（缺流量就算不出曝光类根因） |

```bash
curl -b cookie.txt -X POST http://127.0.0.1:8000/api/ingest/preview \
  -F file=@samples/taobao_orders.csv
```

409 的响应体带 diff：

```json
{"detail": {"code": "changes_need_confirm", "message": "1 行会覆盖已有数据，需要确认后再提交",
            "diff": {"inserted": 0, "updated": 1, "unchanged": 14,
                     "samples": [{"key": "TB20261110002", "changes": [{"field": "qty", "before": 1, "after": 2}]}]}}}
```

## 8. 促销日历（5）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/promo` | 登录 | `{periods[], count, range, promo_days, max_days, kinds}` |
| POST | `/api/promo` | owner | `{name, kind, start_dt, end_dt, note?}` → 201 + `sync` |
| PATCH | `/api/promo/{promo_id}` | owner | 改任一段，旧日期自动清标记 |
| DELETE | `/api/promo/{promo_id}` | owner | 删一段并清标记；删空后清掉全部 |
| POST | `/api/promo/rebuild` | owner | 全量重算（兜底） |

`kind`：`campaign`（平台大促）/ `store`（店铺活动）/ `other`；单段 ≤ 90 天。

## 9. 设置（4，均仅 owner）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | 当前生效配置；key 只回掩码（`sk-591…db05`）与 `has_key` |
| PUT | `/api/settings` | 改一项或多项；**传空串=删除该覆盖**，回到 `.env` |
| POST | `/api/settings/reset` | 清掉全部数据库覆盖 |
| POST | `/api/settings/test` | `{target:"chat"|"creative", base_url?, api_key?, model?}` 用 1 个 token 验 key |

## 10. 错误码

| 码 | HTTP | 含义 |
|---|---|---|
| `unauthorized` | 401 | 没登录 / 会话过期 |
| `invalid_spec` | 400 | QuerySpec 字段或取值不对（含未知字段） |
| `invalid_query` | 400 | SQL 编译或执行失败 |
| `invalid_window` / `invalid_date` | 400 | 窗口或日期不合法 |
| `invalid_critic` / `invalid_mode` / `invalid_role` / `invalid_risk` / `invalid_setting` | 400 | 枚举值不在允许范围 |
| `invalid_question` | 400 | 自然语言问题为空或过长 |
| `invalid_period` | 400 | 促销期不合法 |
| `index_missing` | 400 | 检索索引缺失（先跑 `python -m app.retrieval.build_index`） |
| `empty_update` | 400 | 改账号时一个字段都没传 |
| `sku_not_found` / `promo_not_found` / `run_not_found` | 404 | 目标不存在 |
| `image_missing` | 404 | 这个 SKU 没有主图 |
| `changes_need_confirm` | 409 | 导入会覆盖已有数据，要 `confirm_changes` |
| `undo_refused` | 409 | 这一批不可撤销（已撤过 / 状态不允许） |
| `vision_unavailable` | 503 | 视觉依赖不可用（无 key 且未装本地栈） |
| `invalid_ingest_file` | 400 | 文件太大 / 扩展名不支持 |

## 11. 最小可用调用序列

```bash
BASE=http://127.0.0.1:8000

# 1) 登录
curl -c c.txt -s -X POST $BASE/api/auth/login \
  -H 'content-type: application/json' -d '{"username":"admin","password":"StoreDoctor2026"}' >/dev/null

# 2) 一眼看健康
curl -b c.txt -s "$BASE/api/overview?days=7&as_of=2026-11-17"

# 3) 看诊断（带素材块）
curl -b c.txt -s "$BASE/api/diagnose?as_of=2026-11-17&material=true"

# 4) 问一句（流式）
curl -N -b c.txt -s -X POST $BASE/api/chat \
  -H 'content-type: application/json' -d '{"message":"为什么掉这么多"}'

# 5) 审批 → 执行 → 回滚
curl -b c.txt -s -X POST $BASE/api/actions/act_xxx/approve \
  -H 'content-type: application/json' -d '{"note":"同意"}'
curl -b c.txt -s -X POST $BASE/api/actions/act_xxx/execute
curl -b c.txt -s -X POST $BASE/api/actions/act_xxx/rollback \
  -H 'content-type: application/json' -d '{"reason":"再想想"}'
```
