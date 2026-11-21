/**
 * 素材改进面板：打开后按诊断卡的问题码自动起草一版改进素材（改前/改后对比、
 * 修复清单、改进文案、合规检查），人工核对后落成「换素材」动作（drafted，进审批队列）。
 * 所有类名走 .cr-*，样式在同目录的 styles-creative.css。
 */

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

import { getSettings, requestJson } from "../api";
import { pct, ratio } from "../format";
import "../styles-creative.css";

/* ---------------------------------- 类型 ---------------------------------- */

type CreativeAsset = {
  asset_id: string;
  kind: "image" | "copy";
  path?: string;
  url?: string;
  width?: number;
  height?: number;
  format?: string;
};

type CreativeFix = { issue: string; action: string; detail: string; status: "done" | "manual" };

type ComplianceItem = { code: string; status: "pass" | "warn" | "fail"; detail: string };

type CreativeResult = {
  draft_id: string;
  sku_id: string;
  provider: "local" | "dashscope" | "mock";
  status: "ready" | "degraded";
  target: string;
  before: { image_url?: string; text_coverage?: number; issues?: string[] };
  after: { image_url?: string; text_coverage?: number; issues_left?: string[]; size_chart_present?: boolean };
  assets: CreativeAsset[];
  copywriting: { title: string; selling_points: string[]; description: string; size_text: string };
  compliance: { ok: boolean; items: ComplianceItem[]; blockers: string[] };
  fixes: CreativeFix[];
  elapsed_ms: number;
  note: string;
};

/** apply 返回的动作对象字段比这里多，只声明面板要读的字段 */
type AppliedAction = {
  action_id: string;
  type?: string;
  status?: string;
  summary?: string;
  risk_level?: number;
};

type ApplyResult = { draft_id: string; action: AppliedAction };

/** 面板阶段：只有 ready 才渲染正文，其余两种各自占满内容区 */
type Phase = { kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; data: CreativeResult };

/* -------------------------------- 常量与工具 ------------------------------- */

/** 诊断卡的问题码 → 中文名（目前固定这六个码，未知码原样回落） */
const ISSUE_LABEL: Record<string, string | undefined> = {
  NO_SIZE_CHART: "缺尺码表",
  TEXT_HEAVY: "文字占比偏高",
  PLAIN_WHITE: "纯白底无场景",
  WATERMARK: "主图有水印",
  COLLAGE: "拼接图",
  NO_MODEL: "无模特上身",
};

const PROVIDER_LABEL: Record<string, string | undefined> = {
  local: "本地改图",
  dashscope: "百炼生图",
  mock: "本地回放",
};

const COMPLIANCE_TONE: Record<string, "pass" | "warn" | "fail" | undefined> = {
  pass: "pass",
  warn: "warn",
  fail: "fail",
};

const COMPLIANCE_TEXT: Record<string, string | undefined> = {
  pass: "通过",
  warn: "提示",
  fail: "未通过",
};

function issueName(code: string): string {
  return ISSUE_LABEL[code] ?? code;
}

function providerName(provider: string): string {
  return PROVIDER_LABEL[provider] ?? provider;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function elapsedText(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  return ms < 1000 ? `${Math.round(ms)} 毫秒` : `${(ms / 1000).toFixed(1)} 秒`;
}

/** 文字占比是 0~1 的比例：绝对值走 ratio，变化用 pct 转百分比（pct 接收的是百分数） */
function coverageText(before?: number, after?: number): string {
  const b = typeof before === "number" && Number.isFinite(before) ? before : null;
  const a = typeof after === "number" && Number.isFinite(after) ? after : null;
  const delta = b !== null && a !== null ? `，变化 ${pct((a - b) * 100, 1)}` : "";
  return `文字占比 ${b === null ? "—" : ratio(b, 1)} → ${a === null ? "—" : ratio(a, 1)}${delta}`;
}

function issuesText(list: string[] | undefined): string {
  if (list === undefined) return "—";
  if (list.length === 0) return "无（已全部消除）";
  return list.map(issueName).join("、");
}

/* --------------------------------- 子组件 --------------------------------- */

function Skeleton(): JSX.Element {
  return (
    <div className="cr-skeleton" aria-hidden="true">
      <div className="cr-skeleton__row">
        <div className="cr-skeleton__block" />
        <div className="cr-skeleton__block" />
      </div>
      <div className="cr-skeleton__line" />
      <div className="cr-skeleton__line cr-skeleton__line--short" />
      <div className="cr-skeleton__line" />
    </div>
  );
}

/** 单侧对比图：有地址才渲染 img，404/空地址都落成灰块，不留破图图标 */
function Shot({
  label,
  url,
  alt,
  broken,
  onBroken,
  flag,
}: {
  label: string;
  url?: string;
  alt: string;
  broken: boolean;
  onBroken: () => void;
  flag?: string | null;
}): JSX.Element {
  const showImage = url !== undefined && url.trim() !== "" && !broken;
  return (
    <section className="cr-side">
      <h3 className="cr-side__title">{label}</h3>
      {showImage ? (
        <img className="cr-img" src={url} alt={alt} loading="lazy" decoding="async" onError={onBroken} />
      ) : (
        <div className="cr-img cr-img--empty" role="img" aria-label={`${label}图片不可用`}>
          图片不可用
        </div>
      )}
      {flag ? <p className="cr-meta">{flag}</p> : null}
    </section>
  );
}

function FixList({ fixes }: { fixes: CreativeFix[] }): JSX.Element {
  return (
    <section className="cr-fixes">
      <h3>修复清单</h3>
      {fixes.length === 0 ? (
        <p className="cr-note">本次没有需要修复的素材问题。</p>
      ) : (
        <ul className="cr-fixes__list">
          {fixes.map((fix, index) => (
            <li
              key={`${fix.issue}-${index}`}
              className={fix.status === "manual" ? "cr-fix cr-fix--manual" : "cr-fix cr-fix--done"}
            >
              <span className="cr-fix__issue">{issueName(fix.issue)}</span>
              <span className="cr-fix__arrow" aria-hidden="true">
                →
              </span>
              <span className="cr-fix__detail">{fix.detail || fix.action || "—"}</span>
              {fix.status === "manual" ? <span className="cr-fix__tag">需人工</span> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CopyBlock({ copy }: { copy: CreativeResult["copywriting"] }): JSX.Element {
  return (
    <section className="cr-copy">
      <h3>改进文案</h3>
      <p className="cr-copy__title">{copy.title || "—"}</p>
      {copy.selling_points.length === 0 ? null : (
        <ul className="cr-copy__points">
          {copy.selling_points.map((point, index) => (
            <li key={`${index}-${point}`}>{point}</li>
          ))}
        </ul>
      )}
      <p className="cr-copy__desc">{copy.description || "—"}</p>
      <p className="cr-copy__size">尺码说明：{copy.size_text || "—"}</p>
    </section>
  );
}

function ComplianceBlock({ compliance }: { compliance: CreativeResult["compliance"] }): JSX.Element {
  return (
    <section className="cr-compliance">
      <h3>
        合规检查
        {compliance.ok ? (
          <span className="cr-chip cr-chip--ok">通过</span>
        ) : (
          <span className="cr-chip cr-chip--fail">存在阻断</span>
        )}
      </h3>
      {compliance.items.length === 0 ? (
        <p className="cr-note">暂无合规条目。</p>
      ) : (
        <ul className="cr-compliance__list">
          {compliance.items.map((item, index) => {
            const tone = COMPLIANCE_TONE[item.status] ?? "warn";
            return (
              <li key={`${item.code}-${index}`} className={`cr-compliance__item cr-compliance__item--${tone}`}>
                <span className="cr-compliance__dot" aria-hidden="true" />
                <span className="cr-compliance__code">{item.code}</span>
                <span className="cr-compliance__status">{COMPLIANCE_TEXT[item.status] ?? item.status}</span>
                <span className="cr-compliance__detail">{item.detail}</span>
              </li>
            );
          })}
        </ul>
      )}
      {compliance.ok ? null : (
        <div className="cr-compliance__blockers" role="alert">
          <p className="cr-compliance__blockers-head">合规未通过，暂不能起草换素材动作：</p>
          {compliance.blockers.length === 0 ? (
            <p className="cr-note">后端未给出具体阻断原因，请先人工核查。</p>
          ) : (
            <ul>
              {compliance.blockers.map((blocker, index) => (
                <li key={`${index}-${blocker}`}>{blocker}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

/* --------------------------------- 主组件 --------------------------------- */

export type CreativePanelProps = {
  skuId: string;
  issues: string[];
  onClose: () => void;
  onApplied?: (actionId: string) => void;
};

export default function CreativePanel({ skuId, issues, onClose, onApplied }: CreativePanelProps): JSX.Element {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [engine, setEngine] = useState<"local" | "dashscope">("local");
  const [imageModel, setImageModel] = useState<string>("");
  const [keyReady, setKeyReady] = useState<boolean | null>(null);
  const [broken, setBroken] = useState({ before: false, after: false });
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applied, setApplied] = useState<AppliedAction | null>(null);

  // 生图是「有 key 才能点」的选项：先读一次设置里的 creative 段，拿不到就当未知（不禁用）。
  useEffect(() => {
    let alive = true;
    getSettings()
      .then((snapshot) => {
        if (!alive) return;
        setKeyReady(snapshot.creative.has_key);
        setImageModel(snapshot.creative.edit_model);
      })
      .catch(() => {
        if (alive) setKeyReady(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 父组件每次渲染都可能传一个新数组，这里用「去重排序后的内容串」当依赖，内容不变就不重发请求。
  const issueKey = Array.from(new Set(issues.map((code) => code.trim()).filter(Boolean))).sort().join(",");
  const issueList = issueKey === "" ? [] : issueKey.split(",");

  // 打开 / 换 SKU / 换引擎 / 点重试时起草；attempt 只服务「重试」，issueKey 是内容串而不是数组本身。
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setPhase({ kind: "loading" });
    setBroken({ before: false, after: false });
    setApplied(null);
    setApplyError(null);
    setApplying(false);

    const body = {
      sku_id: skuId,
      issues: issueList,
      target: "main_image",
      provider: engine,
      constraints: {
        max_text_coverage: 0.28,
        add_size_chart: issueList.includes("NO_SIZE_CHART"),
        keep_price_tag: false,
        scene: "auto",
        size: 800,
      },
    };

    requestJson<CreativeResult>("/api/creative/draft", {
      method: "POST",
      body: JSON.stringify(body),
      signal: controller.signal,
    })
      .then((data) => {
        if (alive) setPhase({ kind: "ready", data });
      })
      .catch((err: unknown) => {
        if (!alive || controller.signal.aborted) return;
        setPhase({ kind: "error", message: errorText(err) });
      });

    return () => {
      alive = false;
      controller.abort();
    };
  }, [skuId, issueKey, attempt, engine]);

  // Esc 关闭
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  // 初始焦点落在关闭按钮上，键盘用户不用先 Tab 过整页
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const data = phase.kind === "ready" ? phase.data : null;
  const blocked = data !== null && !data.compliance.ok;
  const canApply = data !== null && !blocked && applied === null && !applying;

  const apply = useCallback(async () => {
    if (phase.kind !== "ready" || applying || applied !== null) return;
    const current = phase.data;
    if (!current.compliance.ok) return;

    setApplying(true);
    setApplyError(null);
    try {
      const reason =
        issueList.length > 0
          ? `按素材诊断修复 ${issueList.length} 项问题（${issueList.map(issueName).join("、")}），起草换素材动作`
          : `按素材诊断起草换素材动作（${skuId}）`;
      const res = await requestJson<ApplyResult>(`/api/creative/${encodeURIComponent(current.draft_id)}/apply`, {
        method: "POST",
        body: JSON.stringify({ actor: "demo", reason }),
      });
      setApplied(res.action);
      onApplied?.(res.action.action_id);
    } catch (err: unknown) {
      setApplyError(errorText(err));
    } finally {
      setApplying(false);
    }
  }, [applied, applying, issueList, onApplied, phase, skuId]);

  // 焦点陷阱：面板内只有按钮类可聚焦元素，Tab 到末尾后回到开头
  const onPanelKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const panel = panelRef.current;
    if (panel === null) return;
    const nodes = panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    }
  };

  return (
    <div
      className="cr-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="cr-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={onPanelKeyDown}
      >
        <header className="cr-head">
          <div className="cr-head__text">
            <h2 className="cr-title" id={titleId}>
              生成改进素材
            </h2>
            <p className="cr-sub">
              <span className="cr-sku">{skuId}</span>
              {data === null ? (
                <span className="cr-chip cr-chip--muted">准备中</span>
              ) : (
                <span className="cr-chip">{providerName(data.provider)}</span>
              )}
              {data !== null && data.status === "degraded" ? (
                <span className="cr-chip cr-chip--warn">已降级</span>
              ) : null}
              {data === null ? null : <span className="cr-meta">耗时 {elapsedText(data.elapsed_ms)}</span>}
            </p>
          </div>
          <button
            type="button"
            className="cr-close"
            ref={closeRef}
            onClick={onClose}
            aria-label="关闭生成改进素材面板"
          >
            关闭
          </button>
        </header>

        <div className="cr-body" aria-busy={phase.kind === "loading"}>
          <div className="cr-engine" role="group" aria-label="改图方式">
            <span className="cr-engine__label">改图方式</span>
            <button
              type="button"
              className={engine === "local" ? "cr-engine__opt cr-engine__opt--on" : "cr-engine__opt"}
              aria-pressed={engine === "local"}
              onClick={() => setEngine("local")}
            >
              本地改图
              <em>不调模型，秒出</em>
            </button>
            <button
              type="button"
              className={engine === "dashscope" ? "cr-engine__opt cr-engine__opt--on" : "cr-engine__opt"}
              aria-pressed={engine === "dashscope"}
              disabled={keyReady === false}
              title={keyReady === false ? "没有百炼 key：到「设置 → 生图」填写后才能用" : undefined}
              onClick={() => setEngine("dashscope")}
            >
              百炼生图
              <em>{imageModel === "" ? "需 key" : imageModel}</em>
            </button>
            {keyReady === false ? (
              <span className="cr-engine__hint">未配置百炼 key，只能本地改图</span>
            ) : (
              <span className="cr-engine__hint">生图约 ¥0.2/张，失败会自动降级本地</span>
            )}
          </div>
          {phase.kind === "loading" ? (
            <>
              <p className="cr-sr-only" role="status">
                正在生成改进素材…
              </p>
              <Skeleton />
            </>
          ) : null}

          {phase.kind === "error" ? (
            <div className="cr-error" role="alert">
              <p className="cr-error__msg">生成失败：{phase.message}</p>
              <p className="cr-note">可以重试；若连续失败，请检查后端生图 provider 的配置与额度。</p>
              <button type="button" className="cr-btn" onClick={() => setAttempt((n) => n + 1)}>
                重试
              </button>
            </div>
          ) : null}

          {data === null ? null : (
            <>
              <div className="cr-compare">
                <Shot
                  label="改之前"
                  url={data.before.image_url}
                  alt={`${skuId} 改进前的主图`}
                  broken={broken.before}
                  onBroken={() => setBroken((state) => (state.before ? state : { ...state, before: true }))}
                />
                <Shot
                  label="改之后"
                  url={data.after.image_url}
                  alt={`${skuId} 改进后的主图`}
                  broken={broken.after}
                  onBroken={() => setBroken((state) => (state.after ? state : { ...state, after: true }))}
                  flag={data.after.size_chart_present ? "尺码表已补齐" : null}
                />
              </div>

              <p className="cr-meta">{coverageText(data.before.text_coverage, data.after.text_coverage)}</p>
              <p className="cr-meta">剩余问题：{issuesText(data.after.issues_left)}</p>

              <FixList fixes={data.fixes} />
              <CopyBlock copy={data.copywriting} />
              <ComplianceBlock compliance={data.compliance} />

              {data.note === "" ? null : <p className="cr-note">备注：{data.note}</p>}
            </>
          )}
        </div>

        <footer className="cr-actions">
          <div className="cr-actions__info">
            {applyError !== null ? (
              <p className="cr-note cr-note--bad" role="alert">
                起草失败：{applyError}
              </p>
            ) : applied !== null ? (
              <p className="cr-note">动作已创建，可在「执行」页审批与执行。</p>
            ) : blocked ? (
              <p className="cr-note">合规未通过，起草按钮已禁用。</p>
            ) : (
              <p className="cr-note">起草后进入审批队列，不会立即替换线上主图。</p>
            )}
          </div>
          <div className="cr-actions__btns">
            <button
              type="button"
              className="cr-btn cr-btn--primary"
              disabled={!canApply}
              title={blocked ? "合规未通过，无法起草" : undefined}
              onClick={() => {
                void apply();
              }}
            >
              {applied !== null ? `已进入审批队列：${applied.action_id}` : applying ? "起草中…" : "起草换素材动作"}
            </button>
            <button type="button" className="cr-btn cr-btn--ghost" onClick={onClose}>
              关闭
            </button>
          </div>
          <p className="cr-sr-only" role="status" aria-live="polite">
            {applied === null ? "" : `已进入审批队列：${applied.action_id}`}
          </p>
        </footer>
      </div>
    </div>
  );
}
