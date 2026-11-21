/**
 * 动作与审计的共用中文文案。
 *
 * 三个地方要用同一套说法：执行台列表、动作详情抽屉、trace 查看器。
 * 放在这里统一维护，避免三处各写一份慢慢跑偏（比如一个叫「已回滚」另一个叫「回滚完成」）。
 */

export const STATUS_TEXT: Record<string, string> = {
  drafted: "待审批",
  approved: "已批准",
  executed: "已执行",
  rejected: "已驳回",
  rolled_back: "已回滚",
};

/** 顶部状态胶囊；key 为空串表示不过滤 */
export const STATUS_TABS: { key: string; label: string }[] = [
  { key: "", label: "全部" },
  { key: "drafted", label: "待审批" },
  { key: "approved", label: "已批准" },
  { key: "executed", label: "已执行" },
  { key: "rolled_back", label: "已回滚" },
  { key: "rejected", label: "已驳回" },
];

/** 审计事件 → 中文；后端建草案时记的是 drafted，兼容 created */
export const EVENT_TEXT: Record<string, string> = {
  created: "创建",
  drafted: "创建",
  approved: "批准",
  rejected: "驳回",
  executed: "执行",
  rolled_back: "回滚",
};

/** 动作类型 → 中文（界面里不出现 adjust_price 这种标识） */
export const TYPE_LABEL: Record<string, string> = {
  adjust_price: "调价",
  adjust_bid: "调出价",
  swap_creative: "换素材",
  issue_coupon: "发券",
  restock_alert: "补货预警",
};

/** 参数键 → 中文 */
export const PARAM_LABEL: Record<string, string> = {
  new_price: "新售价",
  price_tag: "价格带",
  bid: "出价",
  plan_id: "投放计划",
  budget_delta: "加投预算",
  creative_id: "素材编号",
  image_path: "图片文件",
  reason: "原因",
  amount: "券面额",
  threshold: "满减门槛",
  quota: "发放张数",
  budget: "总预算",
  days_of_stock: "可售天数",
  supplier: "供应商",
};

/** 被改动的店铺字段 → 中文 */
export const FIELD_LABEL: Record<string, string> = {
  price: "售价",
  bid: "出价",
  creative: "素材",
  coupon: "优惠券",
  stock: "库存",
};

export const RISK_TEXT: Record<number, string> = {
  0: "低风险（可自动执行）",
  1: "需人审",
  2: "高风险 · 需店长审批",
};

export const ROLE_TEXT: Record<string, string> = {
  staff: "运营",
  manager: "店长",
  owner: "店主",
};

/** 金额类参数，展示时带 ¥ */
export const MONEY_KEYS = new Set(["new_price", "bid", "amount", "budget", "budget_delta", "threshold", "price"]);

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** 时间展示：`2026-11-21T09:08:13+08:00` → `11-21 09:08`（按浏览器本地时区） */
export function fmtTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

export function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status;
}

export function riskText(level: number | null | undefined): string {
  if (level === null || level === undefined) return "—";
  return RISK_TEXT[level] ?? `等级 ${level}`;
}

/** 审计里的操作人：能认出的角色 / 系统代号翻成中文，人名的原样显示 */
const ACTOR_TEXT: Record<string, string> = {
  agent: "智能助手",
  ai: "智能助手",
  assistant: "智能助手",
  system: "系统",
  sys: "系统",
  qa: "系统",
  operator: "店长",
  staff: "运营",
  manager: "店长",
  owner: "店主",
};

export function actorText(actor: string | null | undefined): string {
  const value = (actor ?? "").trim();
  if (value === "") return "—";
  const mapped = ACTOR_TEXT[value.toLowerCase()];
  if (mapped !== undefined) return mapped;
  // 早先联调留下的 t1/t9 一类编号，对使用者没有意义，按「系统」处理
  if (/^t\d+$/i.test(value)) return "系统";
  return value;
}
