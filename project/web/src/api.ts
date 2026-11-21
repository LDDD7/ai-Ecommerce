/** 后端 API 封装 + 响应类型。所有请求走同源 `/api`，开发期由 Vite 代理到 127.0.0.1:8000。 */

export type Health = {
  status: "ok" | "degraded" | string;
  env: string;
  db: { path: string; exists: boolean; orders?: number; last_dt?: string };
  llm: { configured: boolean; model_planner: string };
  catalog_version: number;
  metrics_count: number;
  hint?: string;
};

/** 指标单位：金额 / 计数 / 比率（比率后端是 0~1 的小数） */
export type Unit = "currency" | "count" | "ratio" | string;

export type Kpi = {
  name: string;
  label: string;
  unit: Unit;
  value: number;
  prev: number;
  delta_pct: number;
  good: boolean;
};

export type HealthScore = {
  score: number;
  grade: "A" | "B" | "C" | "D" | string;
  parts: { traffic: number; conversion: number; reputation: number };
};

/** 体检台的规则版异常卡（rules-v0，v1 由 /api/diagnose 产出） */
export type SparkCard = {
  code: string;
  title: string;
  dim: string;
  dim_label: string;
  dim_value: string;
  metric: string;
  cur: number;
  prev: number;
  delta_pct: number;
  impact_gmv: number;
  severity: "high" | "medium" | "low" | string;
};

export type TrendPoint = { dt: string; gmv: number; paid_orders: number };
export type StyleSlice = { style: string; gmv: number; paid_orders: number; cvr: number };

export type Overview = {
  as_of: string;
  window: { days: number; start: string; end: string; prev_start: string; prev_end: string };
  kpis: Kpi[];
  health: HealthScore;
  cards: SparkCard[];
  trend: TrendPoint[];
  by_style: StyleSlice[];
  meta: Record<string, unknown>;
};

export type Evidence = {
  metric: string;
  display: string;
  dim_field: string;
  dim_value: string;
  window: { start: string; end: string };
  baseline: number | null;
  current: number | null;
  delta: number | null;
  delta_pct: number | null;
  z: number | null;
  contribution_pct: number | null;
  unit: Unit;
  source: string;
  note: string | null;
};

export type Candidate = { code: string; title: string; confidence: number; why?: string };

export type MaterialFinding = {
  code: string;
  label: string;
  detail?: string;
  severity?: "high" | "medium" | "low" | string;
  action?: string;
  root_causes?: string[];
};

export type MaterialBlock = {
  score: number | null;
  findings: MaterialFinding[];
  evidence: { kind: string; sku_id: string; role: string; text: string; source: string };
  compare_summary: string;
  source: string;
  degraded: boolean;
};

export type DiagnosisCard = {
  card_id: string;
  code: string;
  title: string;
  headline: string;
  scope: { dim: string; dim_label: string; dim_value: string };
  severity: "high" | "medium" | "low" | string;
  confidence: number;
  primary_metric: string;
  window: { start: string; end: string };
  baseline_window: { start: string; end: string };
  evidence: Evidence[];
  gmv_impact: number | null;
  impact_share: number | null;
  narrative: string;
  caveats: string[];
  candidates: Candidate[];
  competitor_gap?: number | null;
  material?: MaterialBlock | null;
  /** Critic 第二轮对该卡的处置：upgrade / drop / demote / skip */
  replan?: { action: string; reason: string; days: number } | null;
  critic?: {
    verdict?: string;
    confidence_before?: number;
    confidence_after?: number;
    factor?: number;
    objections?: string[];
  } | null;
};

export type Factor = {
  name: string;
  display: string;
  base: number;
  cur: number;
  rel: number;
  log_contrib: number;
  log_share: number;
  money_contrib: number;
  money_share: number;
};

export type Decomposition = {
  subject: string;
  factors: Factor[];
  total_base: number;
  total_cur: number;
  delta: number;
  delta_pct: number;
  log_total: number;
  residual_money: number;
  identity_gap_pct: number;
  method: string;
  summary: string;
};

export type FunnelStep = {
  name: string;
  display: string;
  base: number;
  cur: number;
  rate_base: number;
  rate_cur: number;
  rate_rel: number;
  log_contrib: number;
  share: number;
};

export type Funnel = {
  scope: string;
  steps: FunnelStep[];
  overall_base: number;
  overall_cur: number;
  delta: number;
  delta_pct: number;
  log_total: number;
  residual: number;
  summary: string;
};

export type Diagnose = {
  as_of: string;
  window: { start: string; end: string };
  baseline_window: { start: string; end: string };
  cards: DiagnosisCard[];
  gmv: Decomposition;
  funnel: Funnel;
  meta: {
    engine: string;
    points: number;
    groups: number;
    cards: number;
    cards_before_critic?: number;
    dropped: { dim: string; value: string; reason: string }[];
    promo_share?: { window: number; baseline: number; skew: number } | null;
    critic?: CriticMeta | null;
    elapsed_ms: number;
    wall_ms?: number;
    note?: string;
  };
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { accept: "application/json", ...(init?.body ? { "content-type": "application/json" } : {}) },
  });
  if (!res.ok) {
    // 会话过期/未登录：统一广播，让 App 切回登录页（否则每个页面都要自己处理 401）
    if (res.status === 401) window.dispatchEvent(new Event("sd:unauthorized"));
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown; message?: string };
      const d = body.detail;
      if (typeof d === "string") detail = d;
      else if (d && typeof d === "object" && "message" in d) detail = String((d as { message: unknown }).message);
      else if (typeof body.message === "string") detail = body.message;
    } catch {
      /* 非 JSON 响应，保留状态文本 */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export const getHealth = () => requestJson<Health>("/api/health");

export const getOverview = (days = 7, asOf?: string) => {
  const q = new URLSearchParams({ days: String(days) });
  if (asOf) q.set("as_of", asOf);
  return requestJson<Overview>(`/api/overview?${q.toString()}`);
};

export const getDiagnose = (asOf?: string, windowDays = 7, maxCards = 5, material = true) => {
  const q = new URLSearchParams({
    window_days: String(windowDays),
    max_cards: String(maxCards),
    // 诊断页要展示主图素材块，默认带上；mock 回放不发视觉请求，不拖慢首屏。
    material: String(material),
  });
  if (asOf) q.set("as_of", asOf);
  return requestJson<Diagnose>(`/api/diagnose?${q.toString()}`);
};

/* ---------------------------------- 问诊 ---------------------------------- */

export type ChatIntent = {
  kind: "overview" | "diagnose" | "metric" | string;
  question: string;
  window_days: number;
  as_of?: string | null;
  styles: string[];
  channels: string[];
  metrics: string[];
  sku?: string | null;
  reason?: string;
};

export type ChatStep = { tool: string; args: Record<string, unknown>; why?: string };

export type ChatDiagnosisOverview = {
  kind: "overview";
  as_of: string;
  health: HealthScore;
  kpis: Kpi[];
};

export type ChatDiagnosisCard = {
  kind: "card";
  card_id: string;
  code: string;
  title: string;
  scope: { dim: string; dim_label: string; dim_value: string };
  severity: "high" | "medium" | "low" | string;
  confidence: number;
  gmv_impact: number;
  impact_share: number;
  narrative: string;
};

export type ChatEvidenceEvent = {
  card_id: string;
  metric: string;
  display: string;
  dim_field?: string | null;
  dim_value?: string | null;
  baseline: number;
  current: number;
  delta_pct: number;
  z?: number | null;
  unit?: Unit;
};

/** 建议动作（策略库 W2 契约）：risk 0=低风险可自动，1=需人审，2=高风险必须人审 */
export type PlaybookRisk = 0 | 1 | 2;

export type PlaybookEffect = {
  metric: string;
  /** up=上升，down=下降 */
  direction: "up" | "down" | string;
  /** 比例区间，0~1；展示时转百分比 */
  range: [number, number];
  window_days: number;
  note?: string | null;
};

export type PlaybookAction = {
  card_id: string;
  title: string;
  action_type: string;
  /** 动作类型中文标签，缺失时回落 action_type */
  action_label?: string | null;
  risk: PlaybookRisk;
  score: number;
  reasons: string[];
  /** 可能为 null（该动作没有给出预期效果） */
  expected_effect?: PlaybookEffect | null;
};

/** 一组建议动作，按来源诊断卡聚合；actions 可能为空数组 */
export type PlaybookCard = {
  card_id: string;
  code: string;
  title: string;
  gmv_impact?: number | null;
  actions: PlaybookAction[];
};

export type PlaybookPayload = {
  status: string;
  items: PlaybookCard[];
  meta?: { cards?: number; actions?: number; engine?: string; elapsed_ms?: number } | null;
  /** 旧占位帧（status="not_ready"）带的说明文字，新契约不要求，保留兼容 */
  note?: string | null;
};

export type ReplanItem = {
  card_id: string;
  code: string;
  action: "upgrade" | "drop" | "demote" | "skip" | string;
  reason: string;
};

/** Critic 第二轮：同 scope 扩到 N 天找必需辅证后的处置统计 */
export type ReplanMeta = {
  enabled: boolean;
  days: number;
  checked: number;
  upgrade: number;
  drop: number;
  demote: number;
  skip: number;
  items: ReplanItem[];
  elapsed_ms: number;
  degraded: boolean;
  reason?: string | null;
};

export type CriticMeta = {
  engine?: string;
  checked: number;
  support: number;
  weaken: number;
  reject: number;
  elapsed_ms: number;
  replan?: ReplanMeta | null;
  verdicts?: unknown[];
};

export type ChatEvent =
  | { name: "plan"; data: { intent: ChatIntent; planner: string; steps: ChatStep[] } }
  | { name: "tool_call"; data: { tool: string; args: Record<string, unknown>; why?: string } }
  | {
      name: "tool_result";
      data: { tool: string; ok: boolean; summary?: string; error?: string | null; elapsed_ms: number };
    }
  | { name: "diagnosis"; data: ChatDiagnosisOverview | ChatDiagnosisCard }
  | { name: "evidence"; data: ChatEvidenceEvent }
  | { name: "critic"; data: CriticMeta }
  | { name: "playbook"; data: PlaybookPayload }
  | { name: "token"; data: { text: string } }
  | {
      name: "done";
      data: {
        elapsed_ms: number;
        as_of: string;
        window_days: number;
        tools: string[];
        cards: number;
        playbook_actions?: number;
        report_source?: "llm" | "template" | string;
        report_reason?: string | null;
      };
    }
  | { name: "error"; data: { message: string } };

const EVENT_NAMES = new Set([
  "plan",
  "tool_call",
  "tool_result",
  "diagnosis",
  "evidence",
  "critic",
  "playbook",
  "action_draft",
  "token",
  "done",
  "error",
]);

/** 逐帧解析 `event: X\ndata: {...}\n\n`；返回的解析器可以喂不完整的文本块。 */
function sseParser(onEvent: (e: ChatEvent) => void) {
  let buffer = "";
  return (chunk: string) => {
    buffer += chunk;
    let idx = buffer.indexOf("\n\n");
    while (idx >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let name = "";
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (name && EVENT_NAMES.has(name)) {
        try {
          onEvent({ name, data: JSON.parse(dataLines.join("\n")) } as ChatEvent);
        } catch {
          /* 单帧坏了不影响后续 */
        }
      }
      idx = buffer.indexOf("\n\n");
    }
  };
}

export async function streamChat(
  message: string,
  onEvent: (e: ChatEvent) => void,
  opts: { asOf?: string; windowDays?: number; signal?: AbortSignal } = {},
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({
      message,
      as_of: opts.asOf ?? null,
      window_days: opts.windowDays ?? 7,
    }),
    signal: opts.signal,
  });

  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { message?: string };
      if (typeof body.message === "string") detail = body.message;
    } catch {
      /* 保留状态文本 */
    }
    throw new ApiError(detail, res.status);
  }

  const feed = sseParser(onEvent);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    feed(decoder.decode(value, { stream: true }));
  }
  feed(decoder.decode());
}

/* ---------------------------------- 设置 ---------------------------------- */

export type SettingsField = {
  label: string;
  secret: boolean;
  /** 明文值；secret 字段恒为空串（后端不回明文） */
  value: string;
  /** 展示用：secret 字段是 sk-abc…1234，其余同 value */
  masked: string;
  has_value: boolean;
  source: "db" | "env" | "none";
};

export type ProviderPreset = {
  id: string;
  label: string;
  base_url: string;
  models: string[];
  note: string;
};

export type SettingsSnapshot = {
  fields: Record<string, SettingsField>;
  overrides: string[];
  llm: {
    active: string;
    base_url: string;
    models: { planner: string; fast: string; vision: string };
  };
  creative: {
    provider: string;
    has_key: boolean;
    image_model: string;
    edit_model: string;
    adapter: string;
  };
  providers: ProviderPreset[];
  creative_providers: ProviderPreset[];
  overridable: string[];
  env_file: string;
};

export type SettingsTestResult = {
  ok: boolean;
  target: "chat" | "creative";
  reason?: string;
  model?: string;
  text?: string;
  elapsed_ms?: number;
  tokens?: { prompt: number; completion: number };
  note?: string;
};

export const getSettings = () => requestJson<SettingsSnapshot>("/api/settings");

export const saveSettings = (patch: Record<string, string>) =>
  requestJson<SettingsSnapshot>("/api/settings", {
    method: "PUT",
    body: JSON.stringify(patch),
  });

export const resetSettings = () =>
  requestJson<SettingsSnapshot>("/api/settings/reset", { method: "POST" });

export const testSettings = (payload: {
  target: "chat" | "creative";
  base_url?: string;
  api_key?: string;
  model?: string;
}) =>
  requestJson<SettingsTestResult>("/api/settings/test", {
    method: "POST",
    body: JSON.stringify(payload),
  });

/* ---------------------------------- 动作与审批（T9） ---------------------------------- */

export type ActionRisk = 0 | 1 | 2;

export type ActionTypeSpec = {
  type: string;
  label: string;
  required: string[];
  optional: string[];
  effect_metric: string;
  store_field: string;
  description: string;
  risk_note: string;
};

export type ActionTypesPayload = {
  types: ActionTypeSpec[];
  risk_labels: Record<string, string>;
};

export type ActionEffect = {
  metric?: string;
  /** 预期改善区间（比例，0~1） */
  range?: [number, number];
  est_gmv_lift?: number;
  basis?: string;
};

export type ActionItem = {
  action_id: string;
  trace_id: string;
  store_id: string;
  type: string;
  label: string;
  sku_id: string | null;
  risk_level: ActionRisk;
  risk_label: string;
  status: "drafted" | "approved" | "executed" | "rejected" | "rolled_back" | string;
  idempotency_key: string;
  approver: string | null;
  executed_at: string | null;
  rollback_of: string | null;
  created_at: string;
  baseline?: { price?: number | null; cost?: number | null; bid?: number | null } | null;
  params: Record<string, unknown>;
  reason: string;
  summary: string;
  warnings: string[];
  spec?: { label?: string; store_field?: string; effect_metric?: string } | null;
  effect?: ActionEffect | null;
  /** 只有执行过的动作才有 before / after / diff / snapshot */
  before?: string | null;
  after?: string | null;
  diff?: { field: string; before: string | null; after: string | null } | null;
  snapshot?: { store_id: string; sku_id: string | null; field: string; value: string | null } | null;
  revert?: { reverted: boolean; from?: string | null; to?: string | null; field?: string } | null;
  duplicate?: boolean;
};

export type ActionList = {
  items: ActionItem[];
  /** 当前过滤条件下的命中条数（不受 limit 截断） */
  count: number;
  /** 全量动作条数（不跟随 type 过滤，用于「全部」徽标） */
  total: number;
  /** 按状态计数；跟随 type 过滤，不随 limit 截断 */
  status_counts: Record<string, number>;
};

export type AuditEvent = {
  id: number;
  action_id: string;
  event: string;
  actor: string;
  detail: Record<string, unknown>;
  created_at: string;
};

export type AuditList = { items: AuditEvent[]; count: number };

export type TracePayload = {
  trace_id: string;
  actions: ActionItem[];
  events: AuditEvent[];
  count: number;
};

export type ActionPreview = {
  type: string;
  label: string;
  sku_id?: string | null;
  store_field: string;
  effect_metric: string;
  before: string | null;
  after: string | null;
  side_effects?: string[];
  writes_state?: boolean;
  risk_level: ActionRisk;
  risk_label: string;
  warnings: string[];
};

const qs = (params: Record<string, string | number | undefined | null>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

export const getActionTypes = () => requestJson<ActionTypesPayload>("/api/actions/types");

export const listActions = (p: { status?: string; type?: string; limit?: number } = {}) =>
  requestJson<ActionList>(`/api/actions${qs({ status: p.status, type: p.type, limit: p.limit })}`);

export const getAction = (id: string) => requestJson<ActionItem>(`/api/actions/${encodeURIComponent(id)}`);

export const createAction = (p: {
  type: string;
  sku_id?: string | null;
  params: Record<string, unknown>;
  actor?: string;
  reason?: string;
  trace_id?: string;
  store_id?: string;
}) =>
  requestJson<ActionItem>("/api/actions", {
    method: "POST",
    body: JSON.stringify({ actor: "店长", ...p }),
  });

export const dryRunAction = (p: { type: string; sku_id?: string | null; params: Record<string, unknown> }) =>
  requestJson<ActionPreview>("/api/actions/dry-run", { method: "POST", body: JSON.stringify(p) });

export const approveAction = (id: string, p: { approver: string; role?: "staff" | "manager" | "owner"; note?: string }) =>
  requestJson<ActionItem>(`/api/actions/${encodeURIComponent(id)}/approve`, {
    method: "POST",
    body: JSON.stringify({ role: "manager", ...p }),
  });

export const rejectAction = (id: string, p: { actor: string; reason?: string }) =>
  requestJson<ActionItem>(`/api/actions/${encodeURIComponent(id)}/reject`, {
    method: "POST",
    body: JSON.stringify(p),
  });

export const executeAction = (id: string, p: { actor?: string } = {}) =>
  requestJson<ActionItem>(`/api/actions/${encodeURIComponent(id)}/execute`, {
    method: "POST",
    body: JSON.stringify({ actor: "店长", ...p }),
  });

export const rollbackAction = (id: string, p: { actor?: string } = {}) =>
  requestJson<ActionItem>(`/api/actions/${encodeURIComponent(id)}/rollback`, {
    method: "POST",
    body: JSON.stringify({ actor: "店长", ...p }),
  });

export const getAudit = (p: { action_id?: string; limit?: number } = {}) =>
  requestJson<AuditList>(`/api/audit${qs({ action_id: p.action_id, limit: p.limit })}`);

export const getTrace = (traceId: string) =>
  requestJson<TracePayload>(`/api/trace/${encodeURIComponent(traceId)}`);

/* ---------------------------------------------------------------- 创意工单（T11） */

export type CreativeIssue = "NO_SIZE_CHART" | "TEXT_HEAVY" | "PLAIN_WHITE" | "WATERMARK" | "COLLAGE" | "NO_MODEL";

export type CreativeConstraints = {
  max_text_coverage: number;
  add_size_chart: boolean;
  keep_price_tag: boolean;
  scene: "auto" | "indoor" | "outdoor" | "plain";
  size: number;
};

export type CreativeFix = { issue: string; action: string; detail: string; status: "done" | "manual" };

export type CreativeComplianceItem = { code: string; status: "pass" | "warn" | "fail"; detail: string };

export type CreativeResult = {
  draft_id: string;
  sku_id: string;
  provider: "local" | "dashscope" | "mock";
  status: "ready" | "degraded";
  target: string;
  before: { image_url?: string; text_coverage?: number; issues?: string[] };
  after: { image_url?: string; text_coverage?: number; issues_left?: string[]; size_chart_present?: boolean };
  assets: { asset_id: string; kind: "image" | "copy"; path?: string; url?: string; width?: number; height?: number; format?: string }[];
  copywriting: { title: string; selling_points: string[]; description: string; size_text: string };
  compliance: { ok: boolean; items: CreativeComplianceItem[]; blockers: string[] };
  fixes: CreativeFix[];
  elapsed_ms: number;
  note: string;
  meta?: { wall_ms: number };
};

export type CreativeDraftRequest = {
  sku_id: string;
  issues?: string[];
  target?: "main_image" | "detail_image";
  provider?: string | null;
  constraints?: Partial<CreativeConstraints>;
};

export const createCreativeDraft = (req: CreativeDraftRequest) =>
  requestJson<CreativeResult>("/api/creative/draft", {
    method: "POST",
    body: JSON.stringify({ target: "main_image", ...req }),
  });

export const getCreativeDraft = (draftId: string) =>
  requestJson<CreativeResult>("/api/creative/" + encodeURIComponent(draftId));

export const listCreativeDrafts = (p: { sku_id?: string; limit?: number } = {}) =>
  requestJson<{ items: unknown[]; count: number }>("/api/creative" + qs({ sku_id: p.sku_id, limit: p.limit }));

export const applyCreativeDraft = (draftId: string, p: { actor?: string; reason?: string } = {}) =>
  requestJson<{ draft_id: string; action: ActionItem }>(
    "/api/creative/" + encodeURIComponent(draftId) + "/apply",
    { method: "POST", body: JSON.stringify({ actor: "demo", ...p }) }
  );

/* ---------------------------------------------------------------- 登录与权限 */

export type Role = "staff" | "manager" | "owner";

export type SessionUser = {
  user_id: string;
  username: string;
  display_name: string;
  name: string;
  role: Role | string;
  must_change: boolean;
};

export type UserRow = {
  user_id: string;
  username: string;
  display_name: string;
  name: string;
  role: string;
  disabled: boolean;
  must_change: boolean;
  created_at: string;
  last_login_at: string | null;
};

export const login = (username: string, password: string) =>
  requestJson<{ user: SessionUser; expires_at: string }>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });

export const logout = () => requestJson<{ ok: boolean }>("/api/auth/logout", { method: "POST" });

export const getMe = () => requestJson<{ user: SessionUser; auth_mode: string }>("/api/auth/me");

export const changePassword = (oldPassword: string, newPassword: string) =>
  requestJson<{ ok: boolean }>("/api/auth/password", {
    method: "POST",
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });

export const listUsers = () => requestJson<{ items: UserRow[] }>("/api/auth/users");

export const createUser = (p: {
  username: string;
  password: string;
  role: Role;
  display_name?: string;
}) =>
  requestJson<{ user: SessionUser }>("/api/auth/users", {
    method: "POST",
    body: JSON.stringify(p),
  });

export const patchUser = (
  userId: string,
  p: { role?: Role; disabled?: boolean; password?: string; display_name?: string }
) =>
  requestJson<{ user: UserRow }>("/api/auth/users/" + encodeURIComponent(userId), {
    method: "PATCH",
    body: JSON.stringify(p),
  });

export const ROLE_LABEL: Record<string, string> = {
  staff: "运营",
  manager: "店长",
  owner: "店主",
};

export const roleRank = (role: string): number =>
  role === "owner" ? 3 : role === "manager" ? 2 : role === "staff" ? 1 : 0;
