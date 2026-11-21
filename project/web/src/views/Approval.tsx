import { useCallback, useEffect, useRef, useState } from "react";

import { getActionTypes, listActions, type ActionItem, type ActionList, type ActionTypeSpec } from "../api";
import { STATUS_TABS, fmtTime, statusText } from "../audit-labels";

function riskChipClass(level: ActionItem["risk_level"]): "high" | "medium" | "low" {
  if (level === 2) return "high";
  if (level === 1) return "medium";
  return "low";
}

function Row({ action, onOpen }: { action: ActionItem; onOpen: (actionId: string) => void }): JSX.Element {
  const warnings = action.warnings.length;
  const open = () => onOpen(action.action_id);

  return (
    <li
      className={`ax-row ax-row--${action.status}`}
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
          e.preventDefault();
          open();
        }
      }}
    >
      <span className={`chip chip--${riskChipClass(action.risk_level)}`}>{action.risk_label}</span>
      <div className="ax-row__main">
        <strong>{action.summary}</strong>
        <p className="ax-row__sub">
          {action.label} · {action.sku_id || "全店"} · {statusText(action.status)}
        </p>
      </div>
      <div className="ax-row__end">
        <time className="ax-row__time" dateTime={action.created_at}>
          {fmtTime(action.created_at)}
        </time>
        {warnings > 0 ? (
          <span className="ax-warn" title={action.warnings.join("；")} aria-label={`${warnings} 条警告`}>
            ⚠ {warnings}
          </span>
        ) : null}
      </div>
    </li>
  );
}

export type ApprovalProps = {
  /** 点某一行 → 打开详情抽屉 */
  onOpen: (actionId: string) => void;
  /** 变化时重新拉列表（抽屉里操作完会 +1） */
  refreshKey: number;
};

export default function Approval({ onOpen, refreshKey }: ApprovalProps): JSX.Element {
  const [list, setList] = useState<ActionList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [types, setTypes] = useState<ActionTypeSpec[]>([]);
  // 请求序号：筛选切换很快时，只接受最后一次请求的回包
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await listActions({ status: statusFilter || undefined, type: typeFilter || undefined });
      if (seq === seqRef.current) setList(data);
    } catch (err) {
      if (seq === seqRef.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
    // refreshKey 只作为「重新拉一次」的触发信号，不参与请求参数
  }, [statusFilter, typeFilter, refreshKey]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    let alive = true;
    getActionTypes()
      .then((payload) => {
        if (alive) setTypes(payload.types);
      })
      .catch(() => {
        // 类型清单拉不到不影响主列表，下拉里只剩「全部」
        if (alive) setTypes([]);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 服务端已分开返回：total 是全量条数，count 是本次筛选命中条数（都不受 limit 截断）
  const shown = list?.count ?? 0;
  const allCount = list?.total ?? 0;
  const drafted = list?.status_counts["drafted"] ?? 0;

  return (
    <section className="ax-approval" aria-label="动作审批列表">
      <div className="ax-bar">
        <div className="ax-pills" role="group" aria-label="按状态筛选">
          {STATUS_TABS.map((tab) => {
            const n = tab.key === "" ? allCount : (list?.status_counts[tab.key] ?? 0);
            const on = statusFilter === tab.key;
            return (
              <button
                key={tab.key || "all"}
                type="button"
                className={on ? "ax-pill on" : "ax-pill"}
                aria-pressed={on}
                onClick={() => setStatusFilter(tab.key)}
              >
                {tab.label}
                <span className="ax-pill__n">{n}</span>
              </button>
            );
          })}
        </div>
        <select
          className="ax-select"
          aria-label="按动作类型筛选"
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
        >
          <option value="">全部</option>
          {types.map((t) => (
            <option key={t.type} value={t.type}>
              {t.label}
            </option>
          ))}
        </select>
        <span className="ax-meta">
          共 {shown} 条 · 待审 {drafted} 条
        </span>
      </div>

      {loading ? (
        <ul className="ax-list" aria-busy="true" aria-label="正在加载动作">
          <li className="ax-skeleton" />
          <li className="ax-skeleton" />
          <li className="ax-skeleton" />
        </ul>
      ) : error !== null ? (
        <div className="ax-error" role="alert">
          <span className="ax-error__msg">拉取动作失败：{error}</span>
          <button type="button" className="ax-retry" onClick={() => void load()}>
            重试
          </button>
        </div>
      ) : !list || list.items.length === 0 ? (
        <div className="ax-empty">还没有动作。先在「问诊」里问一句，Agent 会起草待审批动作。</div>
      ) : (
        <ul className="ax-list">
          {list.items.map((action) => (
            <Row key={action.action_id} action={action} onOpen={onOpen} />
          ))}
        </ul>
      )}
    </section>
  );
}
