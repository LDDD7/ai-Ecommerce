import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  approveAction,
  executeAction,
  getAction,
  getAudit,
  rejectAction,
  rollbackAction,
  type ActionItem,
  type AuditEvent,
} from "../api";
import {
  EVENT_TEXT,
  FIELD_LABEL,
  MONEY_KEYS,
  PARAM_LABEL,
  RISK_TEXT,
  ROLE_TEXT,
  TYPE_LABEL,
  actorText,
  fmtTime,
  statusText,
} from "../audit-labels";
import { money } from "../format";

/** 审批角色 */
type Role = "staff" | "manager" | "owner";

/** 操作人记在本地：同一个人下次打开不用重填，也不用看「operator」这种代号 */
const OPERATOR_KEY = "storedoctor.operator";

function readOperator(): string {
  try {
    return localStorage.getItem(OPERATOR_KEY) ?? "店长";
  } catch {
    return "店长";
  }
}

function saveOperator(name: string): void {
  try {
    localStorage.setItem(OPERATOR_KEY, name);
  } catch {
    /* 隐私模式下写不进去，忽略即可 */
  }
}

const rememberedOperator = readOperator();

/** 登录角色 → 审批角色：staff 无审批权，但下拉仍需要一个合法值 */
function roleOfUser(role: string | undefined): Role {
  return role === "staff" || role === "manager" || role === "owner" ? role : "manager";
}

/** 正在进行的操作；null 表示空闲 */
type Pending = "approve" | "reject" | "execute" | "rollback" | null;

function chipTone(level: ActionItem["risk_level"]): "high" | "medium" | "low" {
  if (level === 2) return "high";
  if (level === 1) return "medium";
  return "low";
}

function isRole(value: string): value is Role {
  return value === "staff" || value === "manager" || value === "owner";
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

/** 把审计里的原始值翻译成店里能看懂的说法 */
function auditValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "无";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") {
    if (key === "bid") return `¥${value.toFixed(3)}`;
    if (MONEY_KEYS.has(key)) return `¥${value.toFixed(2)}`;
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? "无" : value.map((item) => String(item)).join("；");
  }
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${PARAM_LABEL[k] ?? FIELD_LABEL[k] ?? k} ${auditValue(k, v)}`)
      .join("，");
  }
  const text = String(value);
  return text === "" ? "未填写" : text;
}

type AuditPart = { label: string; value: string };

/** 审计事件 → 一句人话 + 若干「标签：值」，取代原来的 JSON 串 */
function auditParts(event: string, detail: Record<string, unknown>): { summary: string; parts: AuditPart[] } {
  const parts: AuditPart[] = [];
  const consumed = new Set<string>();
  const type = typeof detail.type === "string" ? detail.type : "";
  const typeLabel = TYPE_LABEL[type] ?? type;
  const sku = typeof detail.sku_id === "string" ? detail.sku_id : "";
  const risk = typeof detail.risk_level === "number" ? detail.risk_level : null;
  const warnings = Array.isArray(detail.warnings) ? detail.warnings : [];

  if (event === "drafted" || event === "created") {
    consumed.add("type");
    consumed.add("sku_id");
    consumed.add("risk_level");
    consumed.add("warnings");
    consumed.add("params");
    const params = detail.params;
    if (params && typeof params === "object" && !Array.isArray(params)) {
      for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
        parts.push({ label: PARAM_LABEL[key] ?? key, value: auditValue(key, value) });
      }
    }
    if (risk !== null) parts.push({ label: "风险", value: RISK_TEXT[risk] ?? `等级 ${risk}` });
    if (warnings.length > 0) parts.push({ label: "提醒", value: auditValue("warnings", warnings) });
    return {
      summary: `提交「${typeLabel || "动作"}」${sku ? `，商品 ${sku}` : ""}`,
      parts,
    };
  }

  if (event === "approved") {
    consumed.add("role");
    consumed.add("risk_level");
    consumed.add("note");
    if (typeof detail.role === "string") {
      parts.push({ label: "审批角色", value: ROLE_TEXT[detail.role] ?? detail.role });
    }
    if (risk !== null) parts.push({ label: "风险", value: RISK_TEXT[risk] ?? `等级 ${risk}` });
    if (typeof detail.note === "string" && detail.note.trim() !== "") {
      parts.push({ label: "备注", value: detail.note });
    }
    return { summary: "审批通过，允许执行", parts };
  }

  if (event === "rejected") {
    consumed.add("reason");
    if (typeof detail.reason === "string" && detail.reason.trim() !== "") {
      parts.push({ label: "驳回原因", value: detail.reason });
    }
    return { summary: "驳回了这个动作，不会执行", parts };
  }

  if (event === "executed") {
    consumed.add("applied");
    consumed.add("diff");
    const diff = (detail.diff ?? null) as Record<string, unknown> | null;
    if (diff) {
      const field = typeof diff.field === "string" ? diff.field : "";
      parts.push({ label: "改动字段", value: FIELD_LABEL[field] ?? field ?? "—" });
      parts.push({ label: "改前", value: diff.before === null || diff.before === undefined || diff.before === "" ? "原来没设置" : auditValue(field, diff.before) });
      parts.push({ label: "改后", value: diff.after === null || diff.after === undefined || diff.after === "" ? "清空" : auditValue(field, diff.after) });
    }
    return {
      summary: detail.applied === false ? "执行了，但店铺状态没有变化" : "执行完成，店铺状态已更新",
      parts,
    };
  }

  if (event === "rolled_back") {
    consumed.add("reason");
    consumed.add("revert");
    const revert = (detail.revert ?? null) as Record<string, unknown> | null;
    if (revert) {
      const field = typeof revert.field === "string" ? revert.field : "";
      parts.push({ label: "恢复字段", value: FIELD_LABEL[field] ?? field ?? "—" });
      parts.push({
        label: "恢复为",
        value: revert.to === null || revert.to === undefined || revert.to === "" ? "原来的空状态" : auditValue(field, revert.to),
      });
      if (typeof revert.from === "string" && revert.from !== "") {
        parts.push({ label: "撤销的值", value: revert.from });
      }
    }
    if (typeof detail.reason === "string" && detail.reason.trim() !== "") {
      parts.push({ label: "原因", value: detail.reason });
    }
    return { summary: "已回滚，店铺状态恢复到执行前", parts };
  }

  // 未知事件：把剩下的键逐条翻译，仍然不展示 JSON
  for (const [key, value] of Object.entries(detail)) {
    if (consumed.has(key)) continue;
    parts.push({ label: PARAM_LABEL[key] ?? FIELD_LABEL[key] ?? key, value: auditValue(key, value) });
  }
  return { summary: "留了一条记录", parts };
}

/** 出价保留 3 位小数 */
function bidText(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `¥${value.toFixed(3)}`;
}

export type ActionDetailProps = {
  actionId: string;
  onClose: () => void;
  /** 任何会改变动作状态的操作成功后调用（父组件据此刷新列表） */
  onChanged: () => void;
  /** 打开这条动作所属 trace 的独立页面（可选；父组件没接就不显示入口） */
  onOpenTrace?: (traceId: string) => void;
  /** 当前登录用户的显示名（审批人/操作人默认值） */
  myName?: string;
  /** 当前登录用户的角色（staff 只能看，不能审批/执行/回滚） */
  myRole?: string;
  /** 是否有审批权限（店长及以上）；false 时操作区只显示说明 */
  canApprove?: boolean;
};

export default function ActionDetail({
  actionId,
  onClose,
  onChanged,
  onOpenTrace,
  myName,
  myRole,
  canApprove = true,
}: ActionDetailProps): JSX.Element {
  const operatorName = (myName ?? "").trim() || rememberedOperator;
  const [action, setAction] = useState<ActionItem | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [opError, setOpError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [approver, setApprover] = useState(operatorName);
  const [role, setRole] = useState<Role>(roleOfUser(myRole));
  const [actor, setActor] = useState(operatorName);

  // 挂载或切换动作时并行拉详情与审计；alive 防止旧请求覆盖新选择
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadError(null);
    setOpError(null);
    setAction(null);
    setAudit([]);
    setPending(null);
    setApprover(operatorName);
    setRole(roleOfUser(myRole));
    setActor(operatorName);

    Promise.all([getAction(actionId), getAudit({ action_id: actionId, limit: 200 })])
      .then(([item, list]) => {
        if (!alive) return;
        setAction(item);
        setAudit(list.items);
      })
      .catch((err: unknown) => {
        if (alive) setLoadError(errorText(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [actionId, operatorName, myRole]);

  // Esc 关闭
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const refreshAudit = useCallback(async () => {
    try {
      const list = await getAudit({ action_id: actionId, limit: 200 });
      setAudit(list.items);
    } catch {
      // 动作已经成功，审计刷新失败不回退本地状态
    }
  }, [actionId]);

  const run = useCallback(
    async (kind: Exclude<Pending, null>, request: () => Promise<ActionItem>) => {
      setPending(kind);
      setOpError(null);
      saveOperator(actor.trim() || "店长");
      try {
        const next = await request();
        // 用接口返回的动作替换本地副本，不再重新 GET
        setAction(next);
        onChanged();
        await refreshAudit();
      } catch (err: unknown) {
        setOpError(errorText(err));
      } finally {
        setPending(null);
      }
    },
    [actor, onChanged, refreshAudit],
  );

  const busy = pending !== null;
  const tone = action === null ? "low" : chipTone(action.risk_level);
  const range = action?.effect?.range;
  const rangeText =
    range === undefined ? "" : `${(range[0] * 100).toFixed(0)}%~${(range[1] * 100).toFixed(0)}%`;

  let controls: JSX.Element | null = null;
  if (action !== null) {
    const needApproval =
      action.status === "drafted" ? "审批" : action.status === "approved" ? "执行" : action.status === "executed" ? "回滚" : "";
    if (!canApprove && needApproval !== "") {
      controls = (
        <p className="axd__note">
          这一步需要「店长」及以上权限；你当前是{ROLE_TEXT[myRole ?? ""] ?? "运营"}，只能查看。
        </p>
      );
    } else if (action.status === "drafted") {
      controls = (
        <>
          <div className="axd__field">
            <label htmlFor="axd-approver">审批人</label>
            <input
              id="axd-approver"
              value={approver}
              disabled={busy}
              onChange={(event) => setApprover(event.currentTarget.value)}
            />
          </div>
          <div className="axd__field">
            <label htmlFor="axd-role">角色</label>
            <select
              id="axd-role"
              value={role}
              disabled={busy}
              onChange={(event) => {
                const next = event.currentTarget.value;
                if (isRole(next)) setRole(next);
              }}
            >
              <option value="staff">员工</option>
              <option value="manager">经理</option>
              <option value="owner">店主</option>
            </select>
          </div>
          <button
            type="button"
            className="axd__btn axd__btn--primary"
            disabled={busy}
            onClick={() => {
              void run("approve", () =>
                approveAction(action.action_id, { approver: approver.trim() || "店长", role }),
              );
            }}
          >
            {busy ? "处理中…" : "批准"}
          </button>
          <div className="axd__field">
            <label htmlFor="axd-actor">操作人</label>
            <input
              id="axd-actor"
              value={actor}
              disabled={busy}
              onChange={(event) => setActor(event.currentTarget.value)}
            />
          </div>
          <button
            type="button"
            className="axd__btn axd__btn--danger"
            disabled={busy}
            onClick={() => {
              void run("reject", () =>
                rejectAction(action.action_id, { actor: actor.trim() || "店长" }),
              );
            }}
          >
            {busy ? "处理中…" : "驳回"}
          </button>
        </>
      );
    } else if (action.status === "approved") {
      controls = (
        <>
          <div className="axd__field">
            <label htmlFor="axd-actor">操作人</label>
            <input
              id="axd-actor"
              value={actor}
              disabled={busy}
              onChange={(event) => setActor(event.currentTarget.value)}
            />
          </div>
          <button
            type="button"
            className="axd__btn axd__btn--primary"
            disabled={busy}
            onClick={() => {
              void run("execute", () => executeAction(action.action_id, { actor: actor.trim() || "店长" }));
            }}
          >
            {busy ? "处理中…" : "执行"}
          </button>
        </>
      );
    } else if (action.status === "executed") {
      controls = (
        <>
          <div className="axd__field">
            <label htmlFor="axd-actor">操作人</label>
            <input
              id="axd-actor"
              value={actor}
              disabled={busy}
              onChange={(event) => setActor(event.currentTarget.value)}
            />
          </div>
          <button
            type="button"
            className="axd__btn axd__btn--ghost"
            disabled={busy}
            onClick={() => {
              void run("rollback", () => rollbackAction(action.action_id, { actor: actor.trim() || "店长" }));
            }}
          >
            {busy ? "处理中…" : "回滚"}
          </button>
        </>
      );
    } else if (action.status === "rejected" || action.status === "rolled_back") {
      controls = <p className="axd__note">已结束，无需操作</p>;
    }
  }

  return (
    <div
      className="axd"
      role="dialog"
      aria-modal="true"
      aria-label={action === null ? "动作详情" : `动作详情：${action.summary}`}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="axd__panel">
        <header className="axd__head">
          <h2 className="axd__title">{action === null ? "动作详情" : action.summary}</h2>
          {action === null ? null : <span className={`chip chip--${tone}`}>{action.risk_label}</span>}
          {action === null ? null : <span className="axd__tag">{statusText(action.status)}</span>}
          <button type="button" className="axd__close" aria-label="关闭" onClick={onClose}>
            关闭
          </button>
        </header>

        {loading ? <p className="axd__note">载入中…</p> : null}
        {!loading && loadError !== null ? (
          <p className="axd__note axd__note--bad">载入失败：{loadError}</p>
        ) : null}

        {!loading && action !== null ? (
          <>
            <dl className="axd__grid">
              <div className="axd__kv">
                <dt>动作编号</dt>
                <dd>{action.action_id}</dd>
              </div>
              <div className="axd__kv">
                <dt>链路</dt>
                <dd>
                  <code className="axd__trace">{action.trace_id}</code>
                  {onOpenTrace === undefined ? null : (
                    <button
                      type="button"
                      className="axd__traceopen"
                      onClick={() => onOpenTrace(action.trace_id)}
                    >
                      查看链路
                    </button>
                  )}
                </dd>
              </div>
              <div className="axd__kv">
                <dt>类型</dt>
                <dd>
                  {action.label}（{action.type}）
                </dd>
              </div>
              <div className="axd__kv">
                <dt>商品</dt>
                <dd>{action.sku_id ?? "全店"}</dd>
              </div>
              <div className="axd__kv">
                <dt>创建时间</dt>
                <dd>{fmtTime(action.created_at)}</dd>
              </div>
              {action.executed_at === null ? null : (
                <>
                  <div className="axd__kv">
                    <dt>执行人</dt>
                    <dd>{action.approver ?? "—"}</dd>
                  </div>
                  <div className="axd__kv">
                    <dt>执行时间</dt>
                    <dd>{fmtTime(action.executed_at)}</dd>
                  </div>
                </>
              )}
            </dl>

            <section className="axd__section">
              <h3>参数</h3>
              {Object.keys(action.params).length > 0 ? (
                <dl className="axd__grid">
                  {Object.entries(action.params).map(([key, value]) => (
                    <div className="axd__kv" key={key}>
                      <dt>{PARAM_LABEL[key] ?? key}</dt>
                      <dd>{auditValue(key, value)}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p className="axd__note">无参数</p>
              )}
            </section>

            {action.baseline === undefined || action.baseline === null ? null : (
              <section className="axd__section">
                <h3>基线</h3>
                <dl className="axd__grid">
                  <div className="axd__kv">
                    <dt>售价</dt>
                    <dd>{money(action.baseline.price, 2)}</dd>
                  </div>
                  <div className="axd__kv">
                    <dt>成本</dt>
                    <dd>{money(action.baseline.cost, 2)}</dd>
                  </div>
                  <div className="axd__kv">
                    <dt>出价</dt>
                    <dd>{bidText(action.baseline.bid)}</dd>
                  </div>
                </dl>
              </section>
            )}

            {action.diff === undefined || action.diff === null ? null : (
              <section className="axd__section">
                <h3>差异</h3>
                <div className="axd__diff">
                  <span className="axd__diffbefore">{action.diff.before ?? "—"}</span>
                  <span className="axd__diffarrow">→</span>
                  <span className="axd__diffafter">{action.diff.after ?? "—"}</span>
                </div>
                <p className="axd__note">改动字段：{FIELD_LABEL[action.diff.field] ?? action.diff.field}</p>
              </section>
            )}

            {action.effect?.metric === undefined ? null : (
              <section className="axd__section">
                <h3>预期效果</h3>
                <p>
                  预期 {action.effect.metric} 改善 {rangeText}
                </p>
                {action.effect.basis === undefined ? null : (
                  <p className="axd__note">{action.effect.basis}</p>
                )}
              </section>
            )}

            {action.warnings.length === 0 ? null : (
              <section className="axd__section">
                <h3>警告</h3>
                <ul className="axd__warn">
                  {action.warnings.map((warning, index) => (
                    <li key={`${index}-${warning}`}>{warning}</li>
                  ))}
                </ul>
              </section>
            )}

            <section className="axd__section">
              <h3>审计</h3>
              {audit.length === 0 ? (
                <p className="axd__note">暂无审计记录</p>
              ) : (
                <ol className="axd__timeline">
                  {audit.map((event) => {
                    const view = auditParts(event.event, event.detail);
                    return (
                      <li key={event.id}>
                        <strong>{EVENT_TEXT[event.event] ?? event.event}</strong>
                        <span>{actorText(event.actor)}</span>
                        <time dateTime={event.created_at}>{fmtTime(event.created_at)}</time>
                        <div className="axd__auditbody">
                          <p className="axd__auditsummary">{view.summary}</p>
                          {view.parts.length === 0 ? null : (
                            <dl className="axd__auditparts">
                              {view.parts.map((part, index) => (
                                <div key={`${part.label}-${index}`} className="axd__auditpart">
                                  <dt>{part.label}</dt>
                                  <dd>{part.value}</dd>
                                </div>
                              ))}
                            </dl>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ol>
              )}
            </section>

            {opError === null ? null : (
              <p className="axd__note axd__note--bad">操作失败：{opError}</p>
            )}

            <div className="axd__actions">{controls}</div>
          </>
        ) : null}
      </div>
    </div>
  );
}
