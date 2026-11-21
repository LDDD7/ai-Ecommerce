// ---- 查数：QuerySpec 编译器 + 自然语言解析（A+B） ----

import { requestJson, type Unit } from "./api";

export type MetricInfo = {
  name: string;
  display: string;
  unit: Unit;
  description: string;
  dimensions: string[];
  source: string;
};

export type CatalogLimits = {
  max_metrics: number;
  max_dimensions: number;
  max_filters: number;
  max_rows: number;
  max_days: number;
};

export type CatalogPayload = {
  version: string;
  owner: string;
  metrics: MetricInfo[];
  limits: CatalogLimits;
};

export type QueryFilter = { field: string; op: string; value: unknown };

export type QuerySpec = {
  metrics: string[];
  dimensions: string[];
  filters: QueryFilter[];
  time: { start: string; end: string };
  order_by?: { field: string; dir: "asc" | "desc" }[];
  limit?: number;
};

export type QueryMeta = {
  elapsed_ms: number;
  row_count: number;
  total_groups: number;
  sources: Record<string, number>;
  metrics: { name: string; display: string; unit: Unit }[];
  sql_statements: number;
  truncated: boolean;
  max_rows: number;
  wall_ms?: number;
};

export type QueryResult = {
  columns: string[];
  rows: (string | number | null)[][];
  meta: QueryMeta;
};

export type QueryNLResult = {
  spec: QuerySpec;
  source: "llm" | "rule";
  warnings: string[];
  elapsed_ms: number;
  question: string;
  as_of: string;
  /** 解析出的窗口长度（天）；在结果顶层，不在 spec 里 */
  window_days: number;
  result: QueryResult | null;
  meta: { wall_ms: number };
};

export const getCatalog = () => requestJson<CatalogPayload>("/api/metrics/catalog");

export const runQuery = (spec: QuerySpec) =>
  requestJson<QueryResult>("/api/query", {
    method: "POST",
    body: JSON.stringify({ spec }),
  });

export const queryNL = (p: { question: string; asOf?: string; windowDays?: number; useLlm?: boolean }) =>
  requestJson<QueryNLResult>("/api/query/nl", {
    method: "POST",
    body: JSON.stringify({
      question: p.question,
      as_of: p.asOf ?? null,
      window_days: p.windowDays ?? 7,
      use_llm: p.useLlm ?? true,
    }),
  });
