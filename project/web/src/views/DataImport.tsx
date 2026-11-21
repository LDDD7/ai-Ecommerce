import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import "../styles-import.css";
import { ApiError } from "../api";
import {
  CommitConflict,
  commitIngest,
  getIngestCapabilities,
  getIngestRuns,
  getIngestSchema,
  previewIngest,
  undoIngest,
  type CommitDiff,
  type CommitResult,
  type IngestCapabilities,
  type IngestPreview,
  type IngestRun,
  type IngestSchema,
} from "../api-ingest";

const VERDICT_LABEL: Record<string, string> = {
  pass: "通过",
  warn: "有警告",
  reject: "不通过",
};

function VerdictPill({ verdict }: { verdict: string }) {
  return <span className={`im-verdict im-verdict--${verdict}`}>{VERDICT_LABEL[verdict] ?? verdict}</span>;
}

export default function DataImport() {
  const [schema, setSchema] = useState<IngestSchema | null>(null);
  const [caps, setCaps] = useState<IngestCapabilities | null>(null);
  const [runs, setRuns] = useState<IngestRun[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [override, setOverride] = useState<string>("");
  const [preview, setPreview] = useState<IngestPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<CommitResult | null>(null);
  const [conflict, setConflict] = useState<CommitDiff | null>(null);
  const [undoArm, setUndoArm] = useState<string | null>(null);
  const [undoing, setUndoing] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    const [nextCaps, nextRuns] = await Promise.all([getIngestCapabilities(), getIngestRuns()]);
    setCaps(nextCaps);
    setRuns(nextRuns.items);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const [nextSchema] = await Promise.all([getIngestSchema(), refresh()]);
        setSchema(nextSchema);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      }
    })();
  }, [refresh]);

  const dataset = useMemo(
    () => schema?.datasets.find((item) => item.name === (preview?.dataset ?? override)) ?? null,
    [schema, preview, override],
  );

  const runPreview = useCallback(
    async (nextFile: File, nextOverride: string, nextMapping: Record<string, string> | null) => {
      setBusy(true);
      setError(null);
      const result = await previewIngest(nextFile, nextOverride || null, nextMapping);
      setBusy(false);
      if (!result.ok) {
        setPreview(null);
        setError(result.message);
        return;
      }
      setPreview(result.data);
      setMapping(result.data.mapping);
    },
    [],
  );

  const onPick = useCallback(
    (picked: File | null) => {
      setDone(null);
      setPreview(null);
      setFile(picked);
      if (picked) void runPreview(picked, override, null);
    },
    [override, runPreview],
  );

  const onMappingChange = useCallback(
    (field: string, column: string) => {
      if (!file) return;
      const next = { ...mapping };
      if (column) next[field] = column;
      else delete next[field];
      setMapping(next);
      void runPreview(file, preview?.dataset ?? override, next);
    },
    [file, mapping, override, preview, runPreview],
  );

  const submit = useCallback(
    async (confirmChanges = false) => {
      if (!preview) return;
      setBusy(true);
      setError(null);
      try {
        const result = await commitIngest(
          preview.token,
          preview.report.verdict !== "reject",
          confirmChanges,
        );
        setDone(result);
        setConflict(null);
        setPreview(null);
        setFile(null);
        setMapping({});
        if (inputRef.current) inputRef.current.value = "";
        await refresh();
      } catch (err) {
        if (err instanceof CommitConflict) {
          setConflict(err.diff);
        } else {
          setError(err instanceof ApiError ? err.message : String(err));
        }
      } finally {
        setBusy(false);
      }
    },
    [preview, refresh],
  );

  const runUndo = useCallback(
    async (runId: string) => {
      if (undoArm !== runId) {
        setUndoArm(runId);
        return;
      }
      setUndoArm(null);
      setUndoing(runId);
      setError(null);
      try {
        await undoIngest(runId);
        setDone(null);
        await refresh();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setUndoing(null);
      }
    },
    [refresh, undoArm],
  );

  const labelOf = useCallback(
    (field: string) => dataset?.fields.find((item) => item.name === field)?.label ?? field,
    [dataset],
  );

  const report = preview?.report;
  const blocked = !preview || preview.report.verdict === "reject";

  return (
    <section className="im">
      <p className="hint">
        把平台或 ERP 导出的文件拖进来：系统先认字段、给出质量报告，确认无误后才写库。同一份文件重复导入是更新，不会把成交额翻倍。
      </p>

      {caps ? (
        <div className="im-cap">
          <span className="im-cap__title">当前数据</span>
          {schema?.capabilities.map((item) => {
            const rows = caps.tables[item.dataset] ?? 0;
            return (
              <span key={item.dataset} className={`im-chip ${rows > 0 ? "im-chip--on" : "im-chip--off"}`} title={rows > 0 ? `${rows} 行` : "还没有"}>
                {item.label}
                <b>{rows > 0 ? rows.toLocaleString("zh-CN") : "缺"}</b>
              </span>
            );
          })}
          {caps.missing_metrics.length > 0 ? (
            <span className="im-cap__missing">缺数据导致这些指标算不出来：{caps.missing_metrics.join("、")}</span>
          ) : null}
        </div>
      ) : null}

      <div className="im-card">
        <div className="im-card__head">
          <h2>导入文件</h2>
          <span className="im-card__hint">支持 {schema?.accepts.join(" / ") ?? ".csv / .xlsx"}，单个文件 ≤ 50MB</span>
        </div>
        <div className="im-pick">
          <input
            ref={inputRef}
            type="file"
            accept={schema?.accepts.join(",") ?? ".csv,.xlsx"}
            onChange={(event) => onPick(event.target.files?.[0] ?? null)}
            aria-label="选择要导入的文件"
          />
          <label className="im-pick__data">
            数据类型
            <select
              value={preview?.dataset ?? override}
              onChange={(event) => {
                const value = event.target.value;
                setOverride(value);
                if (file) void runPreview(file, value, null);
              }}
            >
              <option value="">自动识别</option>
              {schema?.datasets.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {preview ? (
          <div className="im-meta">
            <span>
              认成 <b>{preview.dataset_label}</b>
            </span>
            <span>{preview.row_count} 行</span>
            <span>
              表头在第 {preview.header_row + 1} 行
            </span>
            <span className="im-meta__file">{preview.filename}</span>
            {busy ? <span className="im-meta__busy">识别中…</span> : null}
          </div>
        ) : null}

        {error ? <p className="im-error">{error}</p> : null}

        {preview && dataset ? (
          <div className="im-map">
            <table>
              <thead>
                <tr>
                  <th>规范字段</th>
                  <th>对应列</th>
                  <th>把握</th>
                </tr>
              </thead>
              <tbody>
                {dataset.fields.map((field) => {
                  const guess = preview.detection?.columns.find((column) => column.field === field.name);
                  return (
                    <tr key={field.name} className={field.required ? "is-required" : ""}>
                      <td>
                        <span className="im-map__label">{field.label}</span>
                        {field.required ? <span className="im-map__req">必填</span> : null}
                      </td>
                      <td>
                        <select
                          value={mapping[field.name] ?? ""}
                          onChange={(event) => onMappingChange(field.name, event.target.value)}
                          aria-label={`${field.label}对应哪一列`}
                        >
                          <option value="">（没有这一列）</option>
                          {preview.headers.map((header) => (
                            <option key={header} value={header}>
                              {header}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>
                        {guess && mapping[field.name] === guess.column ? (
                          <span className="im-map__score" title={guess.reason}>
                            {Math.round(guess.confidence * 100)}%
                          </span>
                        ) : mapping[field.name] ? (
                          <span className="im-map__score im-map__score--manual">手动</span>
                        ) : (
                          <span className="im-map__score im-map__score--none">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>

      {report ? (
        <div className="im-card">
          <div className="im-card__head">
            <h2>质量报告</h2>
            <VerdictPill verdict={report.verdict} />
          </div>
          <div className="im-report">
            <div>
              <span>可入库</span>
              <b>{report.rows_ok}</b>
            </div>
            <div>
              <span>被跳过</span>
              <b>{report.rows_bad}</b>
            </div>
            <div>
              <span>重复行</span>
              <b>{(report.duplicate_rate * 100).toFixed(1)}%</b>
            </div>
            <div>
              <span>金额对不上</span>
              <b>{report.identity.checked ? `${report.identity.failed}/${report.identity.checked}` : "未验算"}</b>
            </div>
            <div>
              <span>日期范围</span>
              <b>
                {report.date_range[0] ?? "—"} ~ {report.date_range[1] ?? "—"}
              </b>
            </div>
            <div>
              <span>断档</span>
              <b>{report.gaps} 天</b>
            </div>
          </div>
          {report.reasons.length ? (
            <ul className="im-reasons">
              {report.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
          {report.error_samples.length ? (
            <details className="im-samples">
              <summary>跳过 {report.rows_bad} 行，看几条原因</summary>
              <ul>
                {report.error_samples.map((sample) => (
                  <li key={sample}>{sample}</li>
                ))}
              </ul>
            </details>
          ) : null}
          <div className="im-actions">
            <button type="button" className="im-btn im-btn--primary" onClick={() => void submit()} disabled={blocked || busy || Boolean(conflict)}>
              {busy ? "写入中…" : `确认入库 ${report.rows_ok} 行`}
            </button>
            <span className="im-actions__hint">
              入库前会自动备份当前数据库；若文件里有行会改动已有数据，会先让你确认
            </span>
          </div>
          {conflict ? (
            <div className="im-diff">
              <div className="im-diff__head">
                <b>有 {conflict.updated} 行会覆盖已有数据</b>
                <span>
                  （无变化 {conflict.unchanged} 行，新增 {conflict.inserted} 行）
                </span>
              </div>
              <ul className="im-diff__list">
                {conflict.samples.map((sample) => (
                  <li key={sample.key}>
                    <span className="im-diff__key">{sample.key}</span>
                    {sample.changes.map((change) => (
                      <span key={change.field} className="im-diff__change">
                        {labelOf(change.field)}
                        <s>{String(change.before ?? "—")}</s>
                        <b>{String(change.after ?? "—")}</b>
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
              <div className="im-actions">
                <button type="button" className="im-btn im-btn--primary" onClick={() => void submit(true)} disabled={busy}>
                  {busy ? "写入中…" : "确认覆盖并入库"}
                </button>
                <button type="button" className="im-btn" onClick={() => setConflict(null)} disabled={busy}>
                  取消
                </button>
                <span className="im-actions__hint">确认后仍可用「最近导入」里的「撤销」退回</span>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {done ? (
        <p className="im-done">
          已入库 {done.written} 行（新增 {done.inserted} / 变更 {done.updated} / 无变化{" "}
          {done.unchanged}），跳过 {done.skipped} 行 · 批次 {done.run_id}
          {done.undoable ? "（可撤销）" : ""}
        </p>
      ) : null}

      <div className="im-card">
        <div className="im-card__head">
          <h2>最近导入</h2>
          <span className="im-card__hint">每次导入都可追溯，出问题可以定位到具体文件</span>
        </div>
        {runs.length ? (
          <table className="im-runs">
            <thead>
              <tr>
                <th>时间</th>
                <th>类型</th>
                <th>文件</th>
                <th>行数</th>
                <th>新增 / 变更 / 无变化</th>
                <th>结果</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id} className={run.status === "undone" ? "is-undone" : ""}>
                  <td>{run.created_at.replace("T", " ")}</td>
                  <td>{schema?.datasets.find((item) => item.name === run.dataset)?.label ?? run.dataset}</td>
                  <td className="im-runs__file">{run.filename}</td>
                  <td>{run.rows_written}</td>
                  <td>
                    {run.inserted} / {run.changed ?? run.updated} / {run.unchanged ?? 0}
                  </td>
                  <td>
                    {run.status === "undone" ? (
                      <span className="im-verdict im-verdict--undone">已撤销</span>
                    ) : (
                      <VerdictPill verdict={run.verdict} />
                    )}
                  </td>
                  <td>
                    {run.status === "undone" ? (
                      <span className="im-runs__muted">—</span>
                    ) : (
                      <button
                        type="button"
                        className={`im-btn im-btn--tiny ${undoArm === run.run_id ? "is-armed" : ""}`}
                        onClick={() => void runUndo(run.run_id)}
                        disabled={undoing === run.run_id}
                        title="把这一批新增的行删掉、改动过的行写回原值"
                      >
                        {undoing === run.run_id ? "撤销中…" : undoArm === run.run_id ? "再点一次确认" : "撤销"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="im-empty">还没有导入记录。先拿一份平台导出的订单表试一下。</p>
        )}
      </div>
    </section>
  );
}
