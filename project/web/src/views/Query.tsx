/**
 * 查数：指标目录 + 结构化 QuerySpec + 自然语言解析 + 结果表格。
 * 类名统一 qy-*，样式在 ../styles-query.css。
 * 页面挂载只拉目录，不自动跑查询，避免打开视图就打后端。
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";

import {
  getCatalog,
  queryNL,
  runQuery,
  type CatalogPayload,
  type MetricInfo,
  type QueryResult,
  type QuerySpec,
} from "../api-query";
import { byUnit } from "../format";
import "../styles-query.css";

/* ---------------------------------- 常量 ---------------------------------- */

const DEFAULT_METRICS = ["gmv", "paid_orders"];
const DEFAULT_DIMENSION = "dt";
const DEFAULT_LIMIT = 200;
const PRESET_DAYS = [7, 14, 30, 90];

/** 指标目录的分组顺序；目录若出现未知 source，追加在末尾。 */
const SOURCE_ORDER = ["traffic", "orders", "ads", "reviews", "inventory"];

const SOURCE_LABEL: Record<string, string | undefined> = {
  traffic: "流量",
  orders: "订单",
  ads: "投放",
  reviews: "评价",
  inventory: "库存",
};

/** 结果表头维度名 → 中文。未知维度原样回落。 */
const DIM_LABEL: Record<string, string | undefined> = {
  dt: "日期",
  week: "周",
  month: "月",
  style: "款式",
  channel_id: "渠道",
  sku_id: "商品",
  price_band: "价格带",
  fabric: "面料",
  color: "颜色",
  supplier: "供应商",
  is_promo: "促销",
  dow: "星期",
  season: "季节",
  category_l1: "一级类目",
  category_l2: "二级类目",
};

/* ---------------------------------- 类型 ---------------------------------- */

type CatalogState =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "ready"; data: CatalogPayload };

type InitialSelection = {
  metrics: string[];
  dimensions: string[];
  time: { start: string; end: string };
  days: number;
  limitText: string;
};

type MetricGroup = {
  source: string;
  label: string;
  metrics: MetricInfo[];
};

type ColumnMeta = {
  col: string;
  label: string;
  unit: string | undefined;
  numeric: boolean;
};

/* -------------------------------- 纯工具函数 ------------------------------- */

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function isoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** 含首尾的 N 天窗口；`endIso` 有值时以它为止，否则用今天（中午避开夏令时切换的边界）。 */
function windowFor(days: number, endIso?: string): { start: string; end: string } {
  const parsed = endIso ? new Date(`${endIso}T12:00:00`) : null;
  const end = parsed && !Number.isNaN(parsed.getTime()) ? parsed : new Date();
  end.setHours(12, 0, 0, 0);
  const start = new Date(end);
  start.setDate(start.getDate() - (Math.max(1, days) - 1));
  return { start: isoDate(start), end: isoDate(end) };
}

/** 起止日期之间的自然日数（含首尾）；解析失败返回 null。 */
function spanDays(start: string, end: string): number | null {
  const startMs = Date.parse(`${start}T00:00:00`);
  const endMs = Date.parse(`${end}T00:00:00`);
  if (Number.isNaN(startMs) || Number.isNaN(endMs) || endMs < startMs) return null;
  return Math.round((endMs - startMs) / 86_400_000) + 1;
}

function clampInt(raw: string | number, min: number, max: number, fallback: number): number {
  const parsed = typeof raw === "number" ? raw : Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.round(parsed)));
}

/** 已选指标 dimensions 的交集，顺序沿用第一个指标的声明顺序。 */
function intersectionDims(catalog: CatalogPayload, names: string[]): string[] {
  if (names.length === 0) return [];
  const byName = new Map(catalog.metrics.map((metric) => [metric.name, metric]));
  let acc: string[] | null = null;
  for (const name of names) {
    const info = byName.get(name);
    if (info === undefined) return [];
    const allowed = new Set(info.dimensions);
    acc = acc === null ? [...info.dimensions] : acc.filter((dim) => allowed.has(dim));
    if (acc.length === 0) return [];
  }
  return acc ?? [];
}

function defaultSelection(catalog: CatalogPayload, asOf?: string): InitialSelection {
  const maxMetrics = Math.max(0, catalog.limits.max_metrics);
  const known = new Set(catalog.metrics.map((metric) => metric.name));
  const picked = DEFAULT_METRICS.filter((name) => known.has(name)).slice(0, maxMetrics);
  const metrics = picked.length > 0 ? picked : catalog.metrics.slice(0, maxMetrics).map((metric) => metric.name);
  const allowedDims = intersectionDims(catalog, metrics);
  const dimensions =
    catalog.limits.max_dimensions > 0 && allowedDims.includes(DEFAULT_DIMENSION) ? [DEFAULT_DIMENSION] : [];
  const maxDays = Math.max(1, catalog.limits.max_days);
  const days = Math.min(7, maxDays);
  return {
    metrics,
    dimensions,
    time: windowFor(days, asOf),
    days,
    limitText: String(Math.max(1, Math.min(DEFAULT_LIMIT, catalog.limits.max_rows || DEFAULT_LIMIT))),
  };
}

function formatCell(cell: string | number | null, unit: string | undefined, numeric: boolean): string {
  if (cell === null) return "—";
  if (!numeric) return String(cell);
  const value = typeof cell === "number" ? cell : Number(cell);
  if (Number.isNaN(value)) return String(cell);
  return byUnit(value, unit);
}

function resultMetaText(result: QueryResult): string {
  const { row_count, total_groups, elapsed_ms, sources, sql_statements, truncated } = result.meta;
  const sourceKeys = Object.keys(sources);
  const sourceText = sourceKeys.length > 0 ? sourceKeys.join("/") : "—";
  return `${row_count} 行 · ${total_groups} 组 · 耗时 ${Math.round(elapsed_ms)} ms · 来源 ${sourceText} · SQL ${sql_statements} 条${
    truncated ? "（已截断）" : ""
  }`;
}

/* --------------------------------- 主视图 --------------------------------- */

export default function Query({ asOf }: { asOf?: string } = {}): JSX.Element {
  const titleId = useId();
  const [catalog, setCatalog] = useState<CatalogState>({ phase: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    setCatalog({ phase: "loading" });
    getCatalog()
      .then((data) => {
        if (alive) setCatalog({ phase: "ready", data });
      })
      .catch((err: unknown) => {
        if (alive) setCatalog({ phase: "error", message: errorText(err) });
      });
    return () => {
      alive = false;
    };
  }, [attempt]);

  return (
    <section className="qy-page" aria-labelledby={titleId}>
      <header className="qy-head">
        <h2 className="qy-head__title" id={titleId}>
          查数
        </h2>
        <p className="qy-head__lead">
          自己拼指标和维度，或用一句话让模型翻译成查询；页面打开时不会自动请求数据。
        </p>
      </header>

      {catalog.phase === "loading" ? (
        <div className="qy-card qy-state" aria-busy="true">
          <p className="qy-meta" role="status">
            正在加载指标目录…
          </p>
          <div className="qy-skeleton" aria-hidden="true">
            <div className="qy-skeleton__row" />
            <div className="qy-skeleton__row" />
            <div className="qy-skeleton__row" />
          </div>
        </div>
      ) : catalog.phase === "error" ? (
        <div className="qy-card qy-state" role="alert">
          <h2 className="qy-card__title">指标目录加载失败</h2>
          <p className="qy-error">{catalog.message}</p>
          <button type="button" className="qy-btn" onClick={() => setAttempt((n) => n + 1)}>
            重试
          </button>
        </div>
      ) : (
        <QueryWorkbench catalog={catalog.data} asOf={asOf} />
      )}
    </section>
  );
}

/* -------------------------------- 工作台 --------------------------------- */

/* 过滤器行：编辑器里用字符串表示，提交前再转成 QueryFilter（in/not in 拆数组、between 拆两值）。 */
type FilterRow = { id: string; field: string; op: string; value: string };

const TEXT_OPS = ["=", "!=", "in", "not in", "like"] as const;
const DATE_OPS = ["=", ">=", "<=", "between"] as const;
const TIME_FIELDS = new Set(["dt"]);
const OP_HINT: Record<string, string> = {
  in: "多个值用逗号分隔，例如 甜美,基础",
  "not in": "多个值用逗号分隔",
  like: "可用 % 通配，例如 %甜%",
  between: "起,止，例如 2026-11-01,2026-11-15",
};
const FILTER_OP_LABEL: Record<string, string> = {
  "=": "等于",
  "!=": "不等于",
  in: "属于",
  "not in": "不属于",
  like: "包含",
  ">=": "不早于",
  "<=": "不晚于",
  between: "区间",
};

let filterSeq = 0;
function newFilterRow(field = "", op = "=", value = ""): FilterRow {
  filterSeq += 1;
  return { id: `f${filterSeq}`, field, op, value };
}

function splitValues(text: string): string[] {
  return text
    .split(/[,，]/)
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

/** 把编辑器里的行转成后端认的 QueryFilter；空行直接丢弃。 */
function rowsToFilters(rows: FilterRow[]): QuerySpec["filters"] {
  const out: QuerySpec["filters"] = [];
  for (const row of rows) {
    if (row.field === "" || row.value.trim() === "") continue;
    if (row.op === "in" || row.op === "not in") {
      const values = splitValues(row.value);
      if (values.length > 0) out.push({ field: row.field, op: row.op, value: values });
      continue;
    }
    if (row.op === "between") {
      const values = splitValues(row.value);
      if (values.length === 2) out.push({ field: row.field, op: "between", value: values });
      continue;
    }
    out.push({ field: row.field, op: row.op, value: row.value.trim() });
  }
  return out;
}

/** 反向：把自然语言解析出的 filters 摊回可编辑的行。 */
function filtersToRows(filters: QuerySpec["filters"]): FilterRow[] {
  const rows: FilterRow[] = [];
  for (const filter of filters) {
    const value = Array.isArray(filter.value)
      ? (filter.value as unknown[]).map((item) => String(item)).join(",")
      : String(filter.value ?? "");
    rows.push(newFilterRow(filter.field, filter.op, value));
  }
  return rows;
}

function rowProblem(row: FilterRow): string | null {
  if (row.field === "") return "先选字段";
  if (row.value.trim() === "") return "还没填值";
  if (row.op === "in" || row.op === "not in") {
    return splitValues(row.value).length === 0 ? "至少填一个值" : null;
  }
  if (row.op === "between") return splitValues(row.value).length === 2 ? null : "区间需要两个值，用逗号分隔";
  return null;
}

function QueryWorkbench({ catalog, asOf }: { catalog: CatalogPayload; asOf?: string }): JSX.Element {
  const questionId = useId();
  const [initial] = useState<InitialSelection>(() => defaultSelection(catalog, asOf));

  const [selectedMetrics, setSelectedMetrics] = useState<string[]>(initial.metrics);
  const [selectedDimensions, setSelectedDimensions] = useState<string[]>(initial.dimensions);
  const [time, setTime] = useState(initial.time);
  const [presetDays, setPresetDays] = useState<number | null>(initial.days);
  const [limitText, setLimitText] = useState(initial.limitText);

  // 过滤条件：可手改的行；自然语言解析出的 filters 会摊回这里继续编辑。
  const [filterRows, setFilterRows] = useState<FilterRow[]>([]);
  const [orderBy, setOrderBy] = useState<QuerySpec["order_by"]>(undefined);

  function setFiltersRowsFromSpec(filters: QuerySpec["filters"]) {
    setFilterRows(filtersToRows(filters));
  }

  const [question, setQuestion] = useState("");
  const [nlBusy, setNlBusy] = useState(false);
  const [nlError, setNlError] = useState<string | null>(null);
  const [nlSource, setNlSource] = useState<"llm" | "rule" | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  const [querying, setQuerying] = useState(false);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);
  const [lastSpec, setLastSpec] = useState<QuerySpec | null>(null);

  const [copyState, setCopyState] = useState<"idle" | "done" | "fail">("idle");
  const copyTimer = useRef<number | null>(null);
  const querySeq = useRef(0);

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const limits = catalog.limits;
  const maxMetrics = Math.max(0, limits.max_metrics);
  const maxDimensions = Math.max(0, limits.max_dimensions);
  const maxRows = Math.max(1, limits.max_rows);

  const metricGroups = useMemo<MetricGroup[]>(() => {
    const buckets = new Map<string, MetricInfo[]>();
    for (const metric of catalog.metrics) {
      const bucket = buckets.get(metric.source);
      if (bucket === undefined) buckets.set(metric.source, [metric]);
      else bucket.push(metric);
    }
    const groups: MetricGroup[] = [];
    for (const source of SOURCE_ORDER) {
      const metrics = buckets.get(source);
      if (metrics !== undefined) {
        groups.push({ source, label: SOURCE_LABEL[source] ?? source, metrics });
        buckets.delete(source);
      }
    }
    for (const [source, metrics] of buckets) {
      groups.push({ source, label: SOURCE_LABEL[source] ?? source, metrics });
    }
    return groups;
  }, [catalog]);

  const dimensionOptions = useMemo(() => intersectionDims(catalog, selectedMetrics), [catalog, selectedMetrics]);

  const columnMeta = useMemo<ColumnMeta[]>(() => {
    if (result === null) return [];
    const metaByName = new Map(result.meta.metrics.map((metric) => [metric.name, metric]));
    const catalogByName = new Map(catalog.metrics.map((metric) => [metric.name, metric]));
    return result.columns.map((col) => {
      const metric = metaByName.get(col) ?? catalogByName.get(col);
      return {
        col,
        label: metric?.display ?? DIM_LABEL[col] ?? col,
        unit: metric?.unit,
        numeric: metric !== undefined,
      };
    });
  }, [catalog, result]);

  const presetOptions = PRESET_DAYS.filter((days) => days <= limits.max_days);
  const atMetricLimit = selectedMetrics.length >= maxMetrics;
  const atDimensionLimit = selectedDimensions.length >= maxDimensions;
  const dimsEnabled = selectedMetrics.length > 0 && dimensionOptions.length > 0 && maxDimensions > 0;

  const dimensionNote =
    selectedMetrics.length === 0
      ? "先选至少一个指标。"
      : maxDimensions === 0
        ? "当前目录不允许拆分维度。"
        : dimensionOptions.length === 0
          ? "已选指标没有共用的维度，只能看汇总。"
          : `可选维度取已选指标的交集，共 ${dimensionOptions.length} 个。`;

  // 过滤条件：任一行为空或格式不对就先不让跑，避免把 400 当作「查询结果为空」。
  const filterIssue = useMemo(() => {
    for (const row of filterRows) {
      const problem = rowProblem(row);
      if (problem !== null) return problem;
    }
    return null;
  }, [filterRows]);

  const fieldChoices = useMemo(() => {
    const fields = new Set<string>(dimensionOptions);
    for (const row of filterRows) if (row.field !== "") fields.add(row.field);
    return [...fields].sort((a, b) => a.localeCompare(b));
  }, [dimensionOptions, filterRows]);

  function addFilterRow() {
    setFilterRows((rows) => (rows.length >= limits.max_filters ? rows : [...rows, newFilterRow()]));
  }

  function updateFilterRow(id: string, patch: Partial<FilterRow>) {
    setFilterRows((rows) => rows.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  }

  function removeFilterRow(id: string) {
    setFilterRows((rows) => rows.filter((row) => row.id !== id));
  }

  function toggleMetric(name: string) {
    const isOn = selectedMetrics.includes(name);
    if (!isOn && selectedMetrics.length >= maxMetrics) return;
    const nextMetrics = isOn ? selectedMetrics.filter((metric) => metric !== name) : [...selectedMetrics, name];
    const nextOptions = intersectionDims(catalog, nextMetrics);
    setSelectedMetrics(nextMetrics);
    setSelectedDimensions((prev) => prev.filter((dim) => nextOptions.includes(dim)));
  }

  function toggleDimension(name: string) {
    if (!dimsEnabled) return;
    if (selectedDimensions.includes(name)) {
      setSelectedDimensions(selectedDimensions.filter((dim) => dim !== name));
      return;
    }
    if (selectedDimensions.length >= maxDimensions) return;
    setSelectedDimensions([...selectedDimensions, name]);
  }

  function currentSpec(): QuerySpec {
    const spec: QuerySpec = {
      metrics: selectedMetrics,
      dimensions: selectedDimensions,
      filters: rowsToFilters(filterRows),
      time,
      limit: clampInt(limitText, 1, maxRows, Math.min(DEFAULT_LIMIT, maxRows)),
    };
    const keptOrder = orderBy?.filter((item) => selectedMetrics.includes(item.field));
    if (keptOrder !== undefined && keptOrder.length > 0) spec.order_by = keptOrder;
    return spec;
  }

  function applyParsedSpec(spec: QuerySpec) {
    const known = new Set(catalog.metrics.map((metric) => metric.name));
    const nextMetrics = spec.metrics.filter((name) => known.has(name)).slice(0, maxMetrics);
    const allowedDimensions = intersectionDims(catalog, nextMetrics);
    setSelectedMetrics(nextMetrics);
    setSelectedDimensions(spec.dimensions.filter((dim) => allowedDimensions.includes(dim)).slice(0, maxDimensions));
    setFiltersRowsFromSpec(spec.filters);
    setOrderBy(spec.order_by);
    setTime(spec.time);
    setLimitText(String(clampInt(spec.limit ?? DEFAULT_LIMIT, 1, maxRows, Math.min(DEFAULT_LIMIT, maxRows))));
    const span = spanDays(spec.time.start, spec.time.end);
    setPresetDays(span !== null && span <= limits.max_days ? span : null);
  }

  async function runSpec(spec: QuerySpec) {
    const seq = querySeq.current + 1;
    querySeq.current = seq;
    setLastSpec(spec);
    setQuerying(true);
    setResultError(null);
    try {
      const data = await runQuery(spec);
      if (seq !== querySeq.current) return;
      setResult(data);
    } catch (err: unknown) {
      if (seq !== querySeq.current) return;
      setResult(null);
      setResultError(errorText(err));
    } finally {
      if (seq === querySeq.current) setQuerying(false);
    }
  }

  async function ask() {
    const trimmed = question.trim();
    if (trimmed === "" || nlBusy) return;
    setNlBusy(true);
    setNlError(null);
    setNlSource(null);
    setWarnings([]);
    try {
      const parsed = await queryNL({ question: trimmed, windowDays: presetDays ?? 7 });
      applyParsedSpec(parsed.spec);
      setNlSource(parsed.source);
      setWarnings(parsed.warnings);
      if (parsed.result !== null) {
        // 结果已随解析一起返回：作废还在路上的手动查询，直接展示。
        querySeq.current += 1;
        setQuerying(false);
        setResultError(null);
        setResult(parsed.result);
        setLastSpec(parsed.spec);
      } else {
        await runSpec(parsed.spec);
      }
    } catch (err: unknown) {
      setNlError(errorText(err));
    } finally {
      setNlBusy(false);
    }
  }

  function flashCopy(state: "done" | "fail") {
    setCopyState(state);
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => {
      setCopyState("idle");
      copyTimer.current = null;
    }, 2000);
  }

  async function copySpec() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(currentSpec(), null, 2));
      flashCopy("done");
    } catch {
      flashCopy("fail");
    }
  }

  function runCurrent() {
    if (selectedMetrics.length === 0) return;
    if (filterIssue !== null) return;
    void runSpec(currentSpec());
  }

  function retry() {
    if (lastSpec !== null) void runSpec(lastSpec);
    else runCurrent();
  }

  return (
    <>
      <form
        className="qy-card qy-nl"
        onSubmit={(event) => {
          event.preventDefault();
          void ask();
        }}
      >
        <label className="sr-only" htmlFor={questionId}>
          用一句话描述想查的指标
        </label>
        <div className="qy-nl__row">
          <input
            id={questionId}
            className="qy-input"
            type="text"
            placeholder="例如：近 14 天成交额和转化率按天看"
            value={question}
            autoComplete="off"
            onChange={(event) => setQuestion(event.target.value)}
          />
          <button
            type="submit"
            className="qy-btn qy-btn--primary"
            disabled={nlBusy || question.trim() === ""}
          >
            {nlBusy ? "解读中…" : "解读并查询"}
          </button>
        </div>

        {nlSource !== null || nlError !== null ? (
          <div className="qy-nl__foot">
            {nlSource !== null ? (
              <span className={`qy-badge qy-badge--${nlSource}`}>
                {nlSource === "llm" ? "模型解析" : "规则解析"}
              </span>
            ) : null}
            {nlError !== null ? (
              <span className="qy-error" role="alert">
                解析失败：{nlError}
              </span>
            ) : null}
          </div>
        ) : null}

        {warnings.length > 0 ? (
          <ul className="qy-warnings">
            {warnings.map((warning, index) => (
              <li key={`${index}-${warning}`}>{warning}</li>
            ))}
          </ul>
        ) : null}
      </form>

      <div className="qy-card qy-builder">
        <div className="qy-card__head">
          <h2 className="qy-card__title">构建查询</h2>
          <span className="qy-meta">
            已选 {selectedMetrics.length} 个指标 · {selectedDimensions.length} 个维度
          </span>
        </div>

        <div className="qy-field">
          <div className="qy-field__head">
            <span className="qy-field__label">指标</span>
            <span className="qy-field__count">
              {selectedMetrics.length}/{maxMetrics}
            </span>
          </div>
          {atMetricLimit ? (
            <p className="qy-field__note">已达上限 {maxMetrics} 个指标，先取消一个再选。</p>
          ) : null}
          {metricGroups.map((group) => (
            <div className="qy-group" key={group.source}>
              <p className="qy-group__label">{group.label}</p>
              <div className="qy-chips">
                {group.metrics.map((metric) => {
                  const on = selectedMetrics.includes(metric.name);
                  return (
                    <button
                      key={metric.name}
                      type="button"
                      className={on ? "qy-chip qy-chip--on" : "qy-chip"}
                      aria-pressed={on}
                      disabled={!on && atMetricLimit}
                      title={metric.description || undefined}
                      onClick={() => toggleMetric(metric.name)}
                    >
                      {metric.display}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        <div className="qy-builder__row">
          <div className="qy-field">
            <div className="qy-field__head">
              <span className="qy-field__label">维度</span>
              <span className="qy-field__count">
                {selectedDimensions.length}/{maxDimensions}
              </span>
            </div>
            <p className="qy-field__note">{dimensionNote}</p>
            <div className="qy-chips">
              {dimensionOptions.map((dim) => {
                const on = selectedDimensions.includes(dim);
                return (
                  <button
                    key={dim}
                    type="button"
                    className={on ? "qy-chip qy-chip--on" : "qy-chip"}
                    aria-pressed={on}
                    disabled={!dimsEnabled || (!on && atDimensionLimit)}
                    onClick={() => toggleDimension(dim)}
                  >
                    {DIM_LABEL[dim] ?? dim}
                  </button>
                );
              })}
              <button
                type="button"
                className={selectedDimensions.length === 0 ? "qy-chip qy-chip--on" : "qy-chip"}
                aria-pressed={selectedDimensions.length === 0}
                disabled={!dimsEnabled}
                onClick={() => setSelectedDimensions([])}
              >
                不拆分
              </button>
            </div>
          </div>

          <div className="qy-field">
            <div className="qy-field__head">
              <span className="qy-field__label">过滤条件</span>
              <span className="qy-field__count">
                {filterRows.length} / {limits.max_filters}
              </span>
            </div>
            <p className="qy-field__note">
              只作用于本次查询；自然语言解析出的条件会填到这里，可以继续改。
            </p>
            {filterRows.length === 0 ? (
              <p className="qy-filter__empty">没有过滤条件，查询覆盖所选时间内的全部数据。</p>
            ) : (
              <ul className="qy-filters">
                {filterRows.map((row) => {
                  const ops = TIME_FIELDS.has(row.field) ? DATE_OPS : TEXT_OPS;
                  const problem = rowProblem(row);
                  return (
                    <li className={problem === null ? "qy-filter" : "qy-filter qy-filter--bad"} key={row.id}>
                      <label className="qy-filter__cell">
                        <span className="sr-only">字段</span>
                        <select
                          className="qy-filter__select"
                          value={row.field}
                          onChange={(event) => {
                            const field = event.target.value;
                            const keepOp = field === row.field || TIME_FIELDS.has(field) === TIME_FIELDS.has(row.field);
                            updateFilterRow(row.id, keepOp ? { field } : { field, op: "=" });
                          }}
                        >
                          <option value="">选择字段</option>
                          {fieldChoices.map((field) => (
                            <option key={field} value={field}>
                              {DIM_LABEL[field] ?? field}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="qy-filter__cell qy-filter__cell--op">
                        <span className="sr-only">比较方式</span>
                        <select
                          className="qy-filter__select"
                          value={(ops as readonly string[]).includes(row.op) ? row.op : ops[0]}
                          onChange={(event) => updateFilterRow(row.id, { op: event.target.value })}
                        >
                          {ops.map((op) => (
                            <option key={op} value={op}>
                              {FILTER_OP_LABEL[op] ?? op}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="qy-filter__cell qy-filter__cell--value">
                        <span className="sr-only">值</span>
                        <input
                          className="qy-filter__input"
                          type="text"
                          value={row.value}
                          placeholder={OP_HINT[row.op] ?? "值"}
                          onChange={(event) => updateFilterRow(row.id, { value: event.target.value })}
                        />
                      </label>
                      <button
                        type="button"
                        className="qy-filter__remove"
                        aria-label={`删除 ${row.field === "" ? "这条" : DIM_LABEL[row.field] ?? row.field} 过滤条件`}
                        onClick={() => removeFilterRow(row.id)}
                      >
                        ×
                      </button>
                      {problem === null ? null : <p className="qy-filter__error">{problem}</p>}
                    </li>
                  );
                })}
              </ul>
            )}
            <div className="qy-filters__foot">
              <button
                type="button"
                className="qy-btn qy-btn--ghost"
                onClick={addFilterRow}
                disabled={filterRows.length >= limits.max_filters}
              >
                + 添加条件
              </button>
              {filterRows.length === 0 ? null : (
                <button type="button" className="qy-btn qy-btn--ghost" onClick={() => setFilterRows([])}>
                  清空
                </button>
              )}
              {filterIssue === null ? null : <span className="qy-filter__hint">修好条件才能运行：{filterIssue}</span>}
            </div>
          </div>

          <div className="qy-field">
            <div className="qy-field__head">
              <span className="qy-field__label">时间</span>
              <span className="qy-field__count">上限 {limits.max_days} 天</span>
            </div>
            <div className="qy-presets" role="group" aria-label="时间窗口">
              {presetOptions.map((days) => (
                <button
                  key={days}
                  type="button"
                  className={presetDays === days ? "qy-preset qy-preset--on" : "qy-preset"}
                  aria-pressed={presetDays === days}
                  onClick={() => {
                    setPresetDays(days);
                    setTime(windowFor(days, asOf));
                  }}
                >
                  {days} 天
                </button>
              ))}
            </div>
            <p className="qy-time__range">
              {time.start} ~ {time.end}
            </p>
            <label className="qy-limit">
              <span>行数上限</span>
              <input
                type="number"
                min={1}
                max={maxRows}
                step={1}
                inputMode="numeric"
                value={limitText}
                onChange={(event) => setLimitText(event.target.value)}
                onBlur={() =>
                  setLimitText(
                    String(clampInt(limitText, 1, maxRows, Math.min(DEFAULT_LIMIT, maxRows))),
                  )
                }
              />
            </label>
          </div>
        </div>

        <div className="qy-actions">
          <button
            type="button"
            className="qy-btn qy-btn--primary"
            disabled={selectedMetrics.length === 0 || filterIssue !== null || querying || nlBusy}
            title={filterIssue === null ? undefined : `过滤条件未填完：${filterIssue}`}
            onClick={runCurrent}
          >
            {querying ? "查询中…" : "运行查询"}
          </button>
          <button
            type="button"
            className="qy-btn"
            disabled={selectedMetrics.length === 0}
            onClick={() => {
              void copySpec();
            }}
          >
            {copyState === "done" ? "已复制" : copyState === "fail" ? "复制失败" : "复制 QuerySpec"}
          </button>
        </div>
      </div>

      <section className="qy-card qy-result" aria-live="polite" aria-busy={querying}>
        <div className="qy-card__head">
          <h2 className="qy-card__title">查询结果</h2>
          {querying ? (
            <span className="qy-status" role="status">
              查询中…
            </span>
          ) : null}
        </div>

        {resultError !== null ? (
          <div className="qy-errorbox">
            <p className="qy-error" role="alert">
              查询失败：{resultError}
            </p>
            <button type="button" className="qy-btn qy-btn--small" onClick={retry}>
              重试
            </button>
          </div>
        ) : querying ? (
          <div className="qy-skeleton" aria-hidden="true">
            <div className="qy-skeleton__row" />
            <div className="qy-skeleton__row" />
            <div className="qy-skeleton__row" />
          </div>
        ) : result === null ? (
          <p className="qy-empty">还没有查询结果，选好指标后点「运行查询」。</p>
        ) : (
          <>
            <p
              className="qy-resultmeta"
              title={
                Object.entries(result.meta.sources)
                  .map(([source, rows]) => `${source} ${rows}`)
                  .join("，") || undefined
              }
            >
              {resultMetaText(result)}
            </p>

            {result.rows.length === 0 ? (
              <p className="qy-empty">这段时间没有数据，换个窗口试试</p>
            ) : (
              <div className="qy-tablewrap" tabIndex={0} role="region" aria-label="查询结果表格">
                <table className="qy-table">
                  <thead>
                    <tr>
                      {columnMeta.map((column, index) => (
                        <th
                          key={`${column.col}-${index}`}
                          scope="col"
                          className={column.numeric ? "qy-th qy-th--num" : "qy-th"}
                        >
                          {column.label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, rowIndex) => (
                      <tr key={rowIndex}>
                        {columnMeta.map((column, cellIndex) => {
                          const cell = row[cellIndex] ?? null;
                          const skuFirst = cellIndex === 0 && column.col === "sku_id";
                          return (
                            <td
                              key={`${column.col}-${cellIndex}`}
                              className={
                                skuFirst
                                  ? column.numeric
                                    ? "qy-td qy-td--num qy-td--mono"
                                    : "qy-td qy-td--mono"
                                  : column.numeric
                                    ? "qy-td qy-td--num"
                                    : "qy-td"
                              }
                            >
                              {formatCell(cell, column.unit, column.numeric)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}

        <details className="qy-json">
          <summary>原始查询参数（JSON）</summary>
          <pre>{JSON.stringify(currentSpec(), null, 2)}</pre>
        </details>
      </section>
    </>
  );
}
