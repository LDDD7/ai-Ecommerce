import { useEffect, useRef, useState } from "react";

import {
  streamChat,
  type ChatDiagnosisCard,
  type ChatDiagnosisOverview,
  type ChatEvidenceEvent,
  type ChatIntent,
  type ChatStep,
  type CriticMeta,
  type PlaybookEffect,
  type PlaybookPayload,
} from "../api";
import { money, pct } from "../format";

type ToolLine = {
  tool: string;
  state: "running" | "ok" | "fail";
  summary?: string;
  error?: string;
  ms?: number;
};

type Turn = {
  id: number;
  question: string;
  running: boolean;
  planner?: string;
  intent?: ChatIntent;
  steps: ChatStep[];
  tools: ToolLine[];
  overview?: ChatDiagnosisOverview;
  cards: ChatDiagnosisCard[];
  playbook?: PlaybookPayload;
  critic?: CriticMeta;
  evidence: Record<string, ChatEvidenceEvent[]>;
  text: string;
  error?: string;
  note?: string;
  elapsed?: number;
  reportSource?: string;
  reportReason?: string;
};

const SUGGESTIONS = [
  "这周大盘怎么样",
  "11-17 那周成交额为什么掉了？甜美款是不是有问题",
  "SKU0292 最近退款率多少",
];

const TOOL_LABEL: Record<string, string> = {
  "insights.overview": "读大盘",
  "diagnose.run": "跑诊断",
  "diagnose.decompose": "拆成交额",
  "playbook.search": "配动作",
};

/* ------------------------------- 建议动作 ------------------------------- */

/** 风险等级文案：给人看的安全提示，必须带文字，不能只靠颜色 */
const PB_RISK_LABEL: Record<number, string> = {
  0: "低风险",
  1: "需人审",
  2: "高风险·必须人审",
};

/** 0/1/2 之外的取值不猜等级，按「待确认」显示，避免把高风险误标成低风险 */
const pbRiskLabel = (risk: number) => PB_RISK_LABEL[risk] ?? "风险待确认";

const pbRiskClass = (risk: number) => (PB_RISK_LABEL[risk] ? `pb-risk pb-risk--${risk}` : "pb-risk");

/** 0.15 → 15%，0.123 → 12.3% */
const pbPct = (v: number) => `${Number((v * 100).toFixed(1))}%`;

/** 规划器对外只说「模型规划 / 规则规划」，不露 rule-v1 / llm-v1 这种内部代号 */
const plannerText = (planner?: string): string =>
  planner === "llm-v1" ? "模型规划" : planner ? "规则规划" : "规则规划";

/** 「预期 7 天内 stockout_rate 下降 15%~35%」；字段缺失时降级显示，不抛错 */
function pbEffect(eff: PlaybookEffect): string {
  const dir = eff.direction === "down" ? "下降" : eff.direction === "up" ? "上升" : "变化";
  const win = Number.isFinite(eff.window_days) && eff.window_days > 0 ? ` ${eff.window_days} 天内` : "";
  const span =
    Array.isArray(eff.range) && eff.range.length >= 2 && eff.range.every((v) => Number.isFinite(v))
      ? ` ${pbPct(eff.range[0])}~${pbPct(eff.range[1])}`
      : "";
  const metric = eff.metric || "该指标";
  const note = eff.note ? `（${eff.note}）` : "";
  return `预期${win} ${metric} ${dir}${span}${note}`;
}

/** 二审摘要：Critic 结论 + 第二轮（扩窗复检）处置，一行说完 */
function criticLine(c: CriticMeta): string {
  const head = `已复审 ${c.checked} 张：成立 ${c.support} / 存疑 ${c.weaken} / 撤销 ${c.reject}`;
  const rp = c.replan;
  if (!rp || !rp.enabled) return head;
  const parts = [`升级 ${rp.upgrade}`, `撤掉 ${rp.drop}`, `降权 ${rp.demote}`];
  if (rp.skip) parts.push(`跳过 ${rp.skip}`);
  const tail = `第二轮（${rp.days} 天窗口）${parts.join("、")}`;
  return `${head} · ${tail}${rp.degraded ? " · 复检降级" : ""}`;
}

/** 按诊断卡分组的建议动作；items 为空时整块不渲染 */
function Playbook({ data }: { data: PlaybookPayload }) {
  const groups = data.items ?? [];
  if (groups.length === 0) return null;
  return (
    <div className="pb">
      <p className="pb-head">建议动作</p>
      {groups.map((group) => {
        const actions = group.actions ?? [];
        return (
          <div key={group.card_id} className="pb-group">
            <p className="pb-cardhead">
              <span className="pb-code">{group.code}</span>
              <span>{group.title}</span>
            </p>
            {actions.length > 0 ? (
              <ul className="pb-actions">
                {actions.map((act) => (
                  <li key={`${group.card_id}-${act.card_id}`} className="pb-action">
                    <p className="pb-actionhead">
                      <span className="pb-title">{act.title}</span>
                      <span className="pb-type">{act.action_label ?? act.action_type}</span>
                      <span className={pbRiskClass(act.risk)}>{pbRiskLabel(act.risk)}</span>
                    </p>
                    {act.expected_effect ? <p className="pb-effect">{pbEffect(act.expected_effect)}</p> : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="pb-empty">这张诊断卡暂无匹配的策略动作</p>
            )}
          </div>
        );
      })}
    </div>
  );
}

const emptyTurn = (id: number, question: string): Turn => ({
  id,
  question,
  running: true,
  steps: [],
  tools: [],
  cards: [],
  evidence: {},
  text: "",
});

export default function Chat({ asOf, onOpenDiagnosis }: { asOf: string; onOpenDiagnosis: () => void }) {
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const nextId = useRef(1);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  const patch = (id: number, fn: (t: Turn) => Turn) =>
    setTurns((prev) => prev.map((t) => (t.id === id ? fn(t) : t)));

  const send = async (question: string) => {
    const q = question.trim();
    if (!q || busy) return;
    const id = nextId.current++;
    setInput("");
    setBusy(true);
    setTurns((prev) => [...prev, emptyTurn(id, q)]);
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChat(
        q,
        (event) => {
          patch(id, (t) => {
            switch (event.name) {
              case "plan":
                return { ...t, planner: event.data.planner, intent: event.data.intent, steps: event.data.steps };
              case "tool_call":
                return {
                  ...t,
                  tools: [...t.tools, { tool: event.data.tool, state: "running" }],
                };
              case "tool_result": {
                const idx = t.tools.reduce(
                  (found, line, i) => (line.tool === event.data.tool && line.state === "running" ? i : found),
                  -1,
                );
                if (idx < 0) return t;
                return {
                  ...t,
                  tools: t.tools.map((line, i) =>
                    i === idx
                      ? {
                          ...line,
                          state: event.data.ok ? "ok" : "fail",
                          summary: event.data.summary,
                          error: event.data.error ?? undefined,
                          ms: event.data.elapsed_ms,
                        }
                      : line,
                  ),
                };
              }
              case "diagnosis":
                return event.data.kind === "overview"
                  ? { ...t, overview: event.data }
                  : { ...t, cards: [...t.cards, event.data] };
              case "evidence": {
                const key = event.data.card_id;
                const list = t.evidence[key] ?? [];
                return { ...t, evidence: { ...t.evidence, [key]: [...list, event.data] } };
              }
              case "playbook":
                return { ...t, playbook: event.data, note: event.data.note ?? t.note };
              case "critic":
                return { ...t, critic: event.data };
              case "token":
                return { ...t, text: t.text + event.data.text };
              case "done":
                return {
                  ...t,
                  running: false,
                  elapsed: event.data.elapsed_ms,
                  reportSource: event.data.report_source,
                  reportReason: event.data.report_reason ?? undefined,
                };
              case "error":
                return { ...t, running: false, error: event.data.message };
              default:
                return t;
            }
          });
        },
        { asOf, signal: controller.signal },
      );
      patch(id, (t) => ({ ...t, running: false }));
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      patch(id, (t) => ({ ...t, running: false, error: message }));
    } finally {
      abortRef.current = null;
      setBusy(false);
    }
  };

  return (
    <section className="chat">
      <div className="chat__stream">
        {turns.length === 0 ? (
          <div className="chat__intro">
            <h2>问一句，看它怎么查</h2>
            <p className="note">
              问「为什么掉了」「某款怎么了」，它会先读大盘、再跑诊断、把成交额拆成流量 × 转化 × 客单价，
              每条结论都带指标、维度、时间窗和变化量。诊断日 {asOf}。
            </p>
            <div className="chat__suggest">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" className="link" onClick={() => void send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {turns.map((turn) => (
          <article key={turn.id} className="turn">
            <p className="turn__q">{turn.question}</p>

            {turn.steps.length > 0 ? (
              <ol className="turn__plan">
                {turn.steps.map((step, i) => (
                  <li key={`${step.tool}-${i}`}>
                    <span className="turn__tool">{TOOL_LABEL[step.tool] ?? step.tool}</span>
                    {step.why ? <span className="turn__why">{step.why}</span> : null}
                  </li>
                ))}
              </ol>
            ) : null}

            {turn.tools.length > 0 ? (
              <p className="turn__tools">
                {turn.tools.map((line, i) => (
                  <span key={`${line.tool}-${i}`} className={`status status--${line.state}`}>
                    {TOOL_LABEL[line.tool] ?? line.tool}
                    {line.ms != null ? ` ${Math.round(line.ms)}ms` : ""}
                  </span>
                ))}
              </p>
            ) : null}

            {turn.overview ? (
              <div className="turn__overview">
                健康分 <strong>{turn.overview.health.score}</strong>（{turn.overview.health.grade} 级）·
                {turn.overview.kpis.slice(0, 3).map((kpi) => (
                  <span key={kpi.name} className="turn__kpi">
                    {kpi.label} {money(kpi.value)} <em className={kpi.good ? "up" : "down"}>{pct(kpi.delta_pct)}</em>
                  </span>
                ))}
              </div>
            ) : null}

            {turn.cards.length > 0 ? (
              <div className="turn__cards">
                {turn.cards.slice(0, 5).map((card) => (
                  <div key={card.card_id} className="turn__card">
                    <p className="turn__cardhead">
                      <span className={`chip chip--${card.severity}`}>{card.code}</span>
                      {card.title}
                      <span className="turn__scope">
                        {card.scope.dim_label}「{card.scope.dim_value}」· 置信度 {Math.round(card.confidence * 100)}% · 影响{" "}
                        {money(card.gmv_impact)}
                      </span>
                    </p>
                    <p className="turn__narr">{card.narrative}</p>
                    {(turn.evidence[card.card_id] ?? []).slice(0, 3).map((ev, i) => (
                      <p key={`${ev.metric}-${i}`} className="turn__ev">
                        {ev.display}
                        {ev.dim_value ? `（${ev.dim_value}）` : ""}：{pct(ev.delta_pct)}
                        {ev.z != null ? `，z=${ev.z.toFixed(1)}` : ""}
                      </p>
                    ))}
                  </div>
                ))}
                <button type="button" className="link" onClick={onOpenDiagnosis}>
                  在「诊断与证据」里看全部卡片 →
                </button>
              </div>
            ) : null}

            {turn.critic ? <p className="turn__critic">{criticLine(turn.critic)}</p> : null}

            {turn.playbook ? <Playbook data={turn.playbook} /> : null}

            {turn.text ? (
              <div className="turn__report">
                {turn.text
                  .split("\n")
                  .filter((line) => line.trim().length > 0)
                  .map((line, i) => (
                    <p key={i} className={/^(\d+\.|·)/.test(line.trim()) ? "turn__li" : ""}>
                      {line}
                    </p>
                  ))}
              </div>
            ) : null}

            {turn.running ? <p className="note">正在查…（流式输出）</p> : null}
            {turn.error ? <p className="turn__err">出错了：{turn.error}</p> : null}
            {turn.elapsed != null && !turn.running ? (
              <p className="turn__foot" title={turn.reportReason ?? undefined}>
                用时 {(turn.elapsed / 1000).toFixed(1)}s · {plannerText(turn.planner)} ·{" "}
                {turn.reportSource === "llm" ? "报告由模型改写" : "报告为模板"} · {turn.note ?? ""}
              </p>
            ) : null}
          </article>
        ))}
        <div ref={bottomRef} />
      </div>

      <form
        className="chat__form"
        onSubmit={(e) => {
          e.preventDefault();
          void send(input);
        }}
      >
        <textarea
          value={input}
          rows={2}
          maxLength={500}
          placeholder="例：这周成交额为什么掉了？甜美款是不是有问题"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send(input);
            }
          }}
        />
        {busy ? (
          <button type="button" className="link" onClick={() => abortRef.current?.abort()}>
            停止
          </button>
        ) : (
          <button type="submit" disabled={!input.trim()} aria-label="发送问题" title="发送">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M12 19V5M12 5l-6 6M12 5l6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        )}
      </form>
    </section>
  );
}
