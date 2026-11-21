import { ApiError } from "./api";

const BASE = "";

export type IngestField = {
  name: string;
  label: string;
  kind: string;
  required: boolean;
  aliases: string[];
};

export type IngestDataset = {
  name: string;
  label: string;
  note: string;
  fields: IngestField[];
};

export type IngestSchema = {
  datasets: IngestDataset[];
  capabilities: { dataset: string; label: string; metrics: string[] }[];
  accepts: string[];
  max_bytes: number;
};

export type QualityReport = {
  dataset: string;
  rows_total: number;
  rows_ok: number;
  rows_bad: number;
  error_samples: string[];
  duplicates: number;
  duplicate_rate: number;
  identity: { checked: number; failed: number; failure_rate: number };
  date_range: [string | null, string | null];
  gaps: number;
  verdict: "pass" | "warn" | "reject";
  reasons: string[];
  sample: Record<string, unknown>[];
};

export type IngestPreview = {
  token: string;
  dataset: string;
  dataset_label: string;
  filename: string;
  headers: string[];
  header_row: number;
  row_count: number;
  mapping: Record<string, string>;
  detection: {
    dataset: string;
    columns: { column: string; field: string | null; label: string | null; kind: string | null; confidence: number; reason: string }[];
    missing: string[];
    meta: Record<string, unknown>;
  } | null;
  report: QualityReport;
  preview_rows: Record<string, unknown>[];
};

export type IngestRun = {
  run_id: string;
  dataset: string;
  filename: string;
  rows_total: number;
  rows_written: number;
  inserted: number;
  updated: number;
  changed: number;
  unchanged: number;
  status: "active" | "undone" | string;
  verdict: string;
  created_at: string;
};

export type IngestCapabilities = {
  tables: Record<string, number>;
  available: string[];
  missing: string[];
  missing_metrics: string[];
};

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* 响应不是 JSON：保留状态码 */
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export function getIngestSchema(): Promise<IngestSchema> {
  return json<IngestSchema>("/api/ingest/schema");
}

export function getIngestCapabilities(): Promise<IngestCapabilities> {
  return json<IngestCapabilities>("/api/ingest/capabilities");
}

export function getIngestRuns(): Promise<{ items: IngestRun[]; count: number }> {
  return json<{ items: IngestRun[]; count: number }>("/api/ingest/runs");
}

export type PreviewResult = { ok: true; data: IngestPreview } | { ok: false; message: string };

export async function previewIngest(file: File, dataset: string | null, mapping: Record<string, string> | null): Promise<PreviewResult> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (dataset) form.append("dataset", dataset);
  if (mapping && Object.keys(mapping).length) form.append("mapping", JSON.stringify(mapping));
  try {
    const data = await json<IngestPreview>("/api/ingest/preview", { method: "POST", body: form });
    return { ok: true, data };
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : String(err) };
  }
}

export type CommitDiff = {
  dataset: string;
  rows: number;
  inserted: number;
  updated: number;
  unchanged: number;
  changed_count: number;
  samples: {
    key: string;
    changes: { field: string; before: unknown; after: unknown }[];
  }[];
};

export type CommitResult = {
  run_id: string;
  dataset: string;
  dataset_label: string;
  written: number;
  inserted: number;
  updated: number;
  unchanged: number;
  changed_count: number;
  samples: CommitDiff["samples"];
  undoable: boolean;
  skipped: number;
  backup: string;
  capabilities: IngestCapabilities;
};

export class CommitConflict extends Error {
  readonly diff: CommitDiff;

  constructor(diff: CommitDiff) {
    super(`有 ${diff.updated} 行会覆盖已有数据`);
    this.name = "CommitConflict";
    this.diff = diff;
  }
}

export async function commitIngest(token: string, allowWarn = true, confirmChanges = false): Promise<CommitResult> {
  const response = await fetch(`${BASE}/api/ingest/commit`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ token, allow_warn: allowWarn, confirm_changes: confirmChanges }),
  });
  if (response.status === 409) {
    const body = (await response.json()) as { detail?: { code?: string; diff?: CommitDiff } };
    if (body.detail?.code === "changes_need_confirm" && body.detail.diff) {
      throw new CommitConflict(body.detail.diff);
    }
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* 保留状态码 */
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as CommitResult;
}

export type UndoResult = {
  run_id: string;
  dataset: string;
  deleted: number;
  restored: number;
  capabilities: IngestCapabilities;
};

export function undoIngest(runId: string): Promise<UndoResult> {
  return json<UndoResult>("/api/ingest/undo", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ run_id: runId }),
  });
}
