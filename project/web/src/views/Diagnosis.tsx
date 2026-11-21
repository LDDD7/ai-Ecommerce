import { useState } from "react";
import type { Diagnose, DiagnosisCard, Evidence } from "../api";
import CreativePanel from "./CreativePanel";
import { byUnit, money, pct, ratio } from "../format";

/** 第二轮复审动作的中文短标签 */
const REPLAN_LABEL: Record<string, string> = {
  upgrade: "升级",
  drop: "撤销",
  demote: "降权",
  skip: "跳过",
};

function EvidenceRow({ ev }: { ev: Evidence }) {
  return (
    <tr>
      <td data-label="证据">
        {ev.display}
        <em>
          {ev.dim_field}「{ev.dim_value}」
        </em>
      </td>
      <td className="num" data-label="基线">
        {byUnit(ev.baseline, ev.unit)}
      </td>
      <td className="num" data-label="当期">
        {byUnit(ev.current, ev.unit)}
      </td>
      <td className={ev.delta_pct !== null && ev.delta_pct < 0 ? "num down" : "num up"} data-label="变化">
        {pct(ev.delta_pct)}
      </td>
      <td className="num" data-label="z">
        {ev.z === null ? "—" : ev.z.toFixed(1)}
      </td>
    </tr>
  );
}

type Improve = { skuId: string; issues: string[] };

function MaterialBlock({ card, onImprove }: { card: DiagnosisCard; onImprove?: (req: Improve) => void }) {
  const block = card.material;
  if (!block) return null;
  const sku = card.scope.dim_value;
  const health = block.score === null ? null : Math.round(block.score * 100);
  const issues = block.findings.map((f) => f.code);
  return (
    <div className="material">
      <img
        className="material__shot"
        src={`/api/images/${sku}?role=main`}
        alt={`${sku} 主图`}
        loading="lazy"
        width={104}
        height={104}
      />
      <div className="material__body">
        <p className="material__head">
          素材检查
          {health === null ? null : <span className="material__score">素材健康 {health}</span>}
          {block.degraded ? <span className="material__warn">已降级</span> : null}
          {onImprove ? (
            <button type="button" className="material__improve" onClick={() => onImprove({ skuId: sku, issues })}>
              生成改进素材
            </button>
          ) : null}
        </p>
        {block.findings.length > 0 ? (
          <ul className="material__list">
            {block.findings.map((f) => (
              <li key={f.code} className={`material__item material__item--${f.severity ?? "medium"}`}>
                <strong>{f.label}</strong>
                {f.detail ? <span>{f.detail}</span> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="material__ok">主图没有发现明显素材问题（尺码表、文字占比、场景、水印均合格）。</p>
        )}
        <p className="material__meta">{block.evidence.text}</p>
        {block.compare_summary ? <p className="material__meta">{block.compare_summary}</p> : null}
      </div>
    </div>
  );
}

function Card({ card, rank, onImprove }: { card: DiagnosisCard; rank: number; onImprove?: (req: Improve) => void }) {
  return (
    <article className="dcard">
      <header className="dcard__head">
        <span className={`chip chip--${card.severity}`}>{card.code}</span>
        <div>
          <h3>{card.title}</h3>
          <p className="dcard__scope">
            <span className="dcard__rank">{String(rank).padStart(2, "0")}</span>
            {card.scope.dim_label}「{card.scope.dim_value}」 · 置信度 {Math.round(card.confidence * 100)}% · 主症状{" "}
            {card.primary_metric}
            {card.replan ? (
              <span className={`replan-tag replan-tag--${card.replan.action}`}>
                {" "}
                复审 {REPLAN_LABEL[card.replan.action] ?? card.replan.action}
              </span>
            ) : null}
          </p>
        </div>
        <p className="dcard__impact">
          {card.gmv_impact === null ? "—" : money(card.gmv_impact)}
          <em>{card.impact_share === null ? "" : `占整体降幅 ${ratio(Math.abs(card.impact_share), 0)}`}</em>
        </p>
      </header>

      <p className="dcard__narrative">{card.narrative}</p>

      <MaterialBlock card={card} onImprove={onImprove} />

      <div className="table-wrap" role="region" aria-label={`${card.title} 的证据`} tabIndex={0}>
        <table className="evidence">
          <caption className="sr-only">
            {card.title}（{card.scope.dim_label}「{card.scope.dim_value}」）的证据：基线、当期、变化与 z 值
          </caption>
          <thead>
            <tr>
              <th scope="col">证据（指标 · 维度）</th>
              <th scope="col" className="num">
                基线
              </th>
              <th scope="col" className="num">
                当期
              </th>
              <th scope="col" className="num">
                变化
              </th>
              <th scope="col" className="num">
                z
              </th>
            </tr>
          </thead>
          <tbody>
            {card.evidence.map((ev) => (
              <EvidenceRow key={`${ev.metric}-${ev.dim_field}-${ev.dim_value}`} ev={ev} />
            ))}
          </tbody>
        </table>
      </div>

      {card.candidates.length > 1 ? (
        <p className="dcard__alt">
          备选：
          {card.candidates
            .slice(1)
            .map((c) => `${c.code} ${c.title} ${Math.round(c.confidence * 100)}%`)
            .join(" · ")}
        </p>
      ) : null}

      {card.caveats.length > 0 ? (
        <ul className="caveats">
          {card.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

export default function Diagnosis({
  diagnose,
  onApplied,
}: {
  diagnose: Diagnose;
  onApplied?: (actionId: string) => void;
}) {
  const { gmv, funnel, cards, meta } = diagnose;
  const critic = meta.critic ?? null;
  const [creative, setCreative] = useState<Improve | null>(null);

  return (
    <>
      <section className="panel">
        <h2>成交额恒等式</h2>
        <p className="panel__lead">{gmv.summary}</p>
        <ul className="factors">
          {gmv.factors.map((f) => (
            <li key={f.name}>
              <span className="factors__name">{f.display}</span>
              <span className="factors__rel">{pct(f.rel * 100)}</span>
              <span className="factors__money">{money(f.money_contrib)}</span>
            </li>
          ))}
        </ul>
        <p className="note">
          口径校验：因子连乘与成交额差 {ratio(gmv.identity_gap_pct, 3)}，链式增量残差 {money(gmv.residual_money)}
        </p>
      </section>

      <section className="panel">
        <h2>转化漏斗</h2>
        <p className="panel__lead">{funnel.summary}</p>
        <ol className="funnel">
          {funnel.steps.map((s) => (
            <li key={s.name}>
              <span className="funnel__name">{s.display}</span>
              <span className="funnel__rate">{ratio(s.rate_cur, 3)}</span>
              <span className={s.rate_rel < 0 ? "funnel__rel down" : "funnel__rel up"}>{pct(s.rate_rel * 100)}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className="panel">
        <h2>
          诊断卡
          <em className="panel__count">共 {cards.length} 张</em>
        </h2>
        {cards.length === 0 ? <p className="note">本期没有达到阈值的异常，店铺状态平稳。</p> : null}
        <div className="dcards">
          {cards.map((c, i) => (
            <Card key={c.card_id} card={c} rank={i + 1} onImprove={setCreative} />
          ))}
        </div>
        {critic ? (
          <p className="note">
            反方审查：已复审 {critic.checked} 张（成立 {critic.support} / 存疑 {critic.weaken} / 撤销 {critic.reject}）
            {critic.replan?.enabled
              ? ` · 第二轮（${critic.replan.days} 天窗口）升级 ${critic.replan.upgrade}、撤掉 ${critic.replan.drop}、降权 ${critic.replan.demote}`
              : " · 第二轮已关闭"}
            {critic.replan?.degraded ? " · 复检降级" : ""}
          </p>
        ) : null}
        <p className="note">
          引擎 {meta.engine} · 扫描 {meta.points} 个异常点 / {meta.groups} 组 · 耗时 {meta.elapsed_ms.toFixed(0)}ms
          {meta.promo_share ? ` · 促销偏斜 ${meta.promo_share.skew.toFixed(2)}` : ""}
        </p>
      </section>

      {creative ? (
        <CreativePanel
          skuId={creative.skuId}
          issues={creative.issues}
          onClose={() => setCreative(null)}
          onApplied={(actionId) => {
            setCreative(null);
            onApplied?.(actionId);
          }}
        />
      ) : null}
    </>
  );
}
