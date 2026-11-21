/**
 * Trace 查看器：按 trace_id 把一次问诊产生的动作与审计串起来。
 *
 * 叙事层（报告）负责「讲清楚」，这一页负责「查得动」：谁在什么时候提交了什么、
 * 谁批的、执行改了什么、有没有回滚。完整字段收在「原始数据」里，默认不打扰。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "../styles-trace.css";

import {
  ApiError,
  getTrace,
  listActions,
  type ActionItem,
  type AuditEvent,
  type TracePayload,
} from "../api";
import {
  EVENT_TEXT,
  FIELD_LABEL,
  PARAM_LABEL,
  RISK_TEXT,
  STATUS_TEXT,
  TYPE_LABEL,
  actorText,
  fmtTime,
  statusText,
} from "../audit-labels";
import { money } from "../format";

type Props = {
  initialTraceId?: string | null;
};

/** 「最近」里的一条：trace 标识 + 最后一次动作时间 + 动作数（界面上不直接露 id） */
type Recent = { id: string; at: string; count: number };

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404) return "没有这条 trace：可能是 id 写错了，或者这条链路已经被清理。";
    return err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

/** 审计 detail 里挑几项翻译成人话，作为时间线右侧的摘要 */
function detailSummary(event: AuditEvent): string {
  const detail = (event.detail ?? {}) as Record<string, unknown>;
  const params = (detail.params ?? {}) as Record<string, unknown>;
  const pairs: string[] = [];

  if (typeof detail.type === "string") {
    pairs.push(TYPE_LABEL[detail.type] ?? detail.type);
  }
  if (typeof detail.sku_id === "string" && detail.sku_id !== "") {
    pairs.push(detail.sku_id);
  }
  if (typeof detail.role === "string") {
    pairs.push(`审批角色 ${detail.role}`);
  }
  if (typeof detail.reason === "string" && detail.reason !== "") {
    pairs.push(`原因：${detail.reason}`);
  }
  if (typeof detail.note === "string" && detail.note !== "") {
    pairs.push(`备注：${detail.note}`);
  }
  if (detail.diff && typeof detail.diff === "object") {
    const diff = detail.diff as Record<string, unknown>;
    const field = String(diff.field ?? "");
    pairs.push(
      `改动 ${FIELD_LABEL[field] ?? field}：${diff.before ?? "空"} → ${diff.after ?? "空"}`,
    );
  }
  if (detail.revert && typeof detail.revert === "object") {
    const revert = detail.revert as Record<string, unknown>;
    const field = String(revert.field ?? "");
    pairs.push(`恢复 ${FIELD_LABEL[field] ?? field} → ${revert.to ?? "空"}`);
  }
  const paramKeys = Object.keys(params).slice(0, 4);
  for (const key of paramKeys) {
    const value = params[key];
    const shown =
      typeof value === "number" && (key === "amount" || key === "new_price" || key === "threshold")
        ? money(value)
        : String(value);
    pairs.push(`${PARAM_LABEL[key] ?? key} ${shown}`);
  }
  if (Array.isArray(detail.warnings) && detail.warnings.length > 0) {
    pairs.push(`提醒：${detail.warnings.join("；")}`);
  }
  return pairs.join(" · ");
}

function statusCounts(actions: ActionItem[]): { status: string; count: number }[] {
  const map = new Map<string, number>();
  for (const action of actions) {
    map.set(action.status, (map.get(action.status) ?? 0) + 1);
  }
  return [...map.entries()].map(([status, count]) => ({ status, count }));
}

export function Trace({ initialTraceId }: Props): JSX.Element {
  const [input, setInput] = useState(initialTraceId ?? "");
  const [traceId, setTraceId] = useState(initialTraceId ?? "");
  const [data, setData] = useState<TracePayload | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [recent, setRecent] = useState<Recent[]>([]);
  const seq = useRef(0);

  const load = useCallback(async (id: string) => {
    const want = id.trim();
    if (want === "") {
      setError("先填一个链路编号，或者点上面的最近记录。");
      return;
    }
    const mine = ++seq.current;
    setLoading(true);
    setError("");
    try {
      const payload = await getTrace(want);
      if (mine !== seq.current) return;
      setData(payload);
      setTraceId(want);
    } catch (err) {
      if (mine !== seq.current) return;
      setData(null);
      setError(errorText(err));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    listActions({ limit: 50 })
      .then((payload) => {
        if (!alive) return;
        const seen = new Map<string, Recent>();
        for (const action of payload.items) {
          const id = action.trace_id;
          if (!id) continue;
          const prev = seen.get(id);
          const at = action.executed_at ?? action.created_at;
          if (prev === undefined) {
            seen.set(id, { id, at, count: 1 });
          } else {
            prev.count += 1;
            if (at > prev.at) prev.at = at;
          }
        }
        setRecent([...seen.values()].sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 6));
      })
      .catch(() => {
        /* 最近记录拿不到不影响主流程 */
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (initialTraceId) void load(initialTraceId);
  }, [initialTraceId, load]);

  const timeline = useMemo(() => {
    if (data === null) return [];
    const actionsById = new Map(data.actions.map((action) => [action.action_id, action]));
    return [...data.events]
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
      .map((event) => ({ event, action: actionsById.get(event.action_id) ?? null }));
  }, [data]);

  const span = useMemo(() => {
    if (timeline.length === 0) return { start: "", end: "" };
    return { start: timeline[0].event.created_at, end: timeline[timeline.length - 1].event.created_at };
  }, [timeline]);

  return (
    <section className="tc">
      <header className="tc__head">
        <div>
          <h2 className="tc__title">操作链路</h2>
          <p className="tc__sub">
            一次问诊里起草、审批、执行、回滚的完整轨迹。输入链路编号，或从最近记录里挑一条。
          </p>
        </div>
      </header>

      <div className="tc__bar">
        <label className="tc__label" htmlFor="tc-input">
          链路编号
        </label>
        <input
          id="tc-input"
          className="tc__input"
          value={input}
          placeholder="tr_9f3a1c"
          spellCheck={false}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void load(input);
          }}
        />
        <button className="tc__btn" type="button" disabled={loading} onClick={() => void load(input)}>
          {loading ? "读取中…" : "查看"}
        </button>
      </div>

      {recent.length > 0 ? (
        <div className="tc__recent">
          <span className="tc__recentlabel">最近</span>
          {recent.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`tc__chip${item.id === traceId ? " is-on" : ""}`}
              title={item.id}
              onClick={() => {
                setInput(item.id);
                void load(item.id);
              }}
            >
              {fmtTime(item.at)} · {item.count} 个动作
            </button>
          ))}
        </div>
      ) : null}

      {error !== "" ? <p className="tc__error">{error}</p> : null}

      {data === null ? (
        <p className="tc__empty">
          还没有打开任何 trace。诊断卡上点「查看 trace」，或者执行台点开一个动作，都能拿到 id。
        </p>
      ) : (
        <>
          <div className="tc__summary">
            <div className="tc__cell">
              <span className="tc__k">链路编号</span>
              <code className="tc__v tc__v--mono">{data.trace_id}</code>
            </div>
            <div className="tc__cell">
              <span className="tc__k">动作</span>
              <span className="tc__v">{data.actions.length}</span>
            </div>
            <div className="tc__cell">
              <span className="tc__k">审计事件</span>
              <span className="tc__v">{data.count}</span>
            </div>
            <div className="tc__cell">
              <span className="tc__k">时间</span>
              <span className="tc__v">
                {fmtTime(span.start)} → {fmtTime(span.end)}
              </span>
            </div>
            <div className="tc__cell tc__cell--widen">
              <span className="tc__k">状态</span>
              <span className="tc__v tc__chips">
                {statusCounts(data.actions).map(({ status, count }) => (
                  <span key={status} className={`tc__status tc__status--${status}`}>
                    {STATUS_TEXT[status] ?? status} {count}
                  </span>
                ))}
              </span>
            </div>
          </div>

          <ol className="tc__timeline">
            {timeline.map(({ event, action }, index) => (
              <li key={`${event.event}-${event.created_at}-${index}`} className="tc__event">
                <time className="tc__time" dateTime={event.created_at}>
                  {fmtTime(event.created_at)}
                </time>
                <span className={`tc__tag tc__tag--${event.event}`}>{EVENT_TEXT[event.event] ?? event.event}</span>
                <div className="tc__body">
                  <p className="tc__line">
                    <strong>{action === null ? "（动作已不存在）" : action.label}</strong>
                    {action !== null && action.sku_id !== "" ? <span className="tc__sku">{action.sku_id}</span> : null}
                    <span className="tc__actor">操作人 {actorText(event.actor)}</span>
                  </p>
                  <p className="tc__detail">{detailSummary(event)}</p>
                </div>
              </li>
            ))}
          </ol>

          <h3 className="tc__h3">这条链路里的动作</h3>
          <div className="tc__tablewrap">
            <table className="tc__table">
              <thead>
                <tr>
                  <th scope="col">动作</th>
                  <th scope="col">类型</th>
                  <th scope="col">商品</th>
                  <th scope="col">状态</th>
                  <th scope="col">风险</th>
                  <th scope="col">创建</th>
                  <th scope="col">执行</th>
                </tr>
              </thead>
              <tbody>
                {data.actions.map((action) => (
                  <tr key={action.action_id}>
                    <td>
                      <code className="tc__v--mono">{action.action_id.slice(-8)}</code>
                    </td>
                    <td>{TYPE_LABEL[action.type] ?? action.type}</td>
                    <td>{action.sku_id || "全店"}</td>
                    <td>
                      <span className={`tc__status tc__status--${action.status}`}>{statusText(action.status)}</span>
                    </td>
                    <td>{RISK_TEXT[action.risk_level] ?? `等级 ${action.risk_level}`}</td>
                    <td>{fmtTime(action.created_at)}</td>
                    <td>{fmtTime(action.executed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <details className="tc__raw">
            <summary>原始数据（JSON）</summary>
            <pre>{JSON.stringify(data, null, 2)}</pre>
          </details>
        </>
      )}
    </section>
  );
}
