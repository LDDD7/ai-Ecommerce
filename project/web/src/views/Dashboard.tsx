import type { Diagnose, DiagnosisCard, Overview } from "../api";
import { compactMoney, count, money, pct, ratio, tone } from "../format";

type Props = {
  overview: Overview;
  diagnose: Diagnose;
  onOpenDiagnosis: () => void;
};

/** 「2026-11-11」→「11 月 11 日」。诊断书的日期要像打印出来的抬头，不要 ISO。 */
function md(iso: string): string {
  const [, m, d] = iso.split("-");
  if (!m || !d) return iso;
  return `${Number(m)} 月 ${Number(d)} 日`;
}

function Sparkline({ points }: { points: { dt: string; gmv: number }[] }) {
  if (points.length < 2) return null;
  const w = 760;
  const h = 90;
  const values = points.map((p) => p.gmv);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = w / (points.length - 1);
  const y = (v: number) => h - ((v - min) / span) * (h - 10) - 5;
  const line = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${y(p.gmv).toFixed(1)}`)
    .join(" ");
  const last = points[points.length - 1];
  const prev = points[points.length - 2];
  return (
    <figure className="spark">
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`近 ${points.length} 天成交额走势`}>
        <defs>
          <linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--blue)" stopOpacity="0.16" />
            <stop offset="1" stopColor="var(--blue)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={`${line} L${w},${h} L0,${h} Z`} fill="url(#spark-fill)" stroke="none" />
        <path d={line} fill="none" stroke="var(--ink)" strokeWidth="1.6" strokeLinejoin="round" />
        <line
          x1={w}
          y1={0}
          x2={w}
          y2={h}
          stroke="var(--line-strong)"
          strokeWidth="1"
          strokeDasharray="3 4"
        />
        <circle className="spark__dot" cx={w} cy={y(last.gmv)} r="3.5" fill="var(--seal)" />
      </svg>
      <figcaption>
        近 {points.length} 天成交额 {compactMoney(min)} ~ {compactMoney(max)}，末日{" "}
        {compactMoney(last.gmv)}
        <em>
          较前一日 {pct(prev.gmv === 0 ? 0 : ((last.gmv - prev.gmv) / prev.gmv) * 100)}
        </em>
      </figcaption>
    </figure>
  );
}

function CardRow({ card, onOpen }: { card: DiagnosisCard; onOpen: () => void }) {
  return (
    <button type="button" className={`card-row card-row--${card.severity}`} onClick={onOpen}>
      <span className={`chip chip--${card.severity}`}>{card.code}</span>
      <span className="card-row__main">
        <span className="card-row__title">
          {card.title}
          <em>
            {card.scope.dim_label}「{card.scope.dim_value}」
          </em>
        </span>
        <span className="card-row__body">{card.narrative}</span>
      </span>
      <span className="card-row__impact">
        {card.gmv_impact === null ? "—" : money(card.gmv_impact)}
        <em>{card.gmv_impact === null ? "" : `占降幅 ${ratio(Math.abs(card.impact_share ?? 0), 0)}`}</em>
      </span>
    </button>
  );
}

/** 诊断书抬头：一句话结论 + 红笔圈注 + 数据落款。整页唯一的「动情处」。 */
function Verdict({
  overview,
  diagnose,
  onOpenDiagnosis,
}: {
  overview: Overview;
  diagnose: Diagnose;
  onOpenDiagnosis: () => void;
}) {
  const gmv = overview.kpis.find((k) => k.name === "gmv");
  const drop = gmv?.delta_pct ?? 0;
  const down = drop < -0.5;
  const top = diagnose.cards[0];
  const { start, end, prev_start, prev_end } = overview.window;
  const impactTotal = diagnose.cards.reduce((sum, c) => sum + Math.abs(c.gmv_impact ?? 0), 0);

  return (
    <section className="verdict" aria-labelledby="verdict-line">
      <p className="verdict__eyebrow">
        <span className="verdict__kicker">经营诊断书</span>
        <span>
          {md(start)} – {md(end)}
        </span>
        <span className="verdict__vs">对比 {md(prev_start)} – {md(prev_end)}</span>
      </p>

      <h2 id="verdict-line" className="verdict__line">
        {down ? "成交额比前一周少了" : drop > 0.5 ? "成交额比前一周多了" : "成交额与前一周持平"}
        {Math.abs(drop) > 0.5 && (
          <span className="verdict__mark">
            {Math.abs(drop).toFixed(1)}
            <i>%</i>
          </span>
        )}
        {top ? (
          down ? (
            <>
              ，最要紧的一处是<strong>{top.title}</strong>（{top.scope.dim_label}「
              {top.scope.dim_value}」），占降幅 {ratio(Math.abs(top.impact_share ?? 0), 0)}。
            </>
          ) : (
            <>
              ；仍有一处值得先处理：<strong>{top.title}</strong>（{top.scope.dim_label}「
              {top.scope.dim_value}」），影响约 {money(top.gmv_impact ?? 0)}。
            </>
          )
        ) : (
          <>；本期没有需要立刻处理的异常，按常规节奏看款即可。</>
        )}
      </h2>

      <dl className="verdict__stats">
        <div>
          <dt>本期成交额</dt>
          <dd title={money(gmv?.value ?? 0)}>
            {compactMoney(gmv?.value ?? 0)}
            <small>上期 {compactMoney(gmv?.prev ?? 0)}</small>
          </dd>
        </div>
        <div>
          <dt>影响合计</dt>
          <dd title={money(-impactTotal)}>{compactMoney(-impactTotal)}</dd>
        </div>
        <div>
          <dt>诊断卡</dt>
          <dd>
            {diagnose.cards.length}
            <small>张 / {diagnose.meta.points} 个异常点</small>
          </dd>
        </div>
        <div>
          <dt>恒等式校验</dt>
          <dd>
            {diagnose.gmv.identity_gap_pct.toFixed(3)}
            <small>% 偏差</small>
          </dd>
        </div>
      </dl>

      <p className="verdict__foot">
        <span>
          口径：{overview.window.days} 天窗口 vs 前 {overview.window.days} 天，成交额 = 流量 × 转化率 × 客单价
        </span>
        <button type="button" className="link" onClick={onOpenDiagnosis}>
          展开全部证据 →
        </button>
      </p>
    </section>
  );
}

export default function Dashboard({ overview, diagnose, onOpenDiagnosis }: Props) {
  const { kpis, trend, by_style: byStyle } = overview;
  const cards = diagnose.cards.slice(0, 3);
  const maxStyle = Math.max(...byStyle.map((s) => s.gmv), 1);

  return (
    <>
      <Verdict overview={overview} diagnose={diagnose} onOpenDiagnosis={onOpenDiagnosis} />

      <section className="panel">
        <h2>本期大盘</h2>
        <dl className="kpis">
          {kpis.map((k, i) => (
            <div key={k.name} className="kpi" style={{ animationDelay: `${i * 45}ms` }}>
              <dt>{k.label}</dt>
              <dd
                className="kpi__value"
                title={k.unit === "currency" ? money(k.value) : undefined}
              >
                {k.unit === "currency" ? compactMoney(k.value) : k.unit === "ratio" ? ratio(k.value) : count(k.value)}
              </dd>
              <dd className={`kpi__delta kpi__delta--${tone(k.delta_pct, k.name === "refund_rate")}`}>
                {pct(k.delta_pct)}
                <em>上期 {k.unit === "currency" ? compactMoney(k.prev) : k.unit === "ratio" ? ratio(k.prev) : count(k.prev)}</em>
              </dd>
            </div>
          ))}
        </dl>
        <Sparkline points={trend} />
      </section>

      <section className="panel">
        <h2>
          诊断结论
          <button type="button" className="link" onClick={onOpenDiagnosis}>
            全部 {diagnose.cards.length} 张 →
          </button>
        </h2>
        <div className="card-list">
          {cards.map((c) => (
            <CardRow key={c.card_id} card={c} onOpen={onOpenDiagnosis} />
          ))}
        </div>
      </section>

      <section className="panel">
        <h2>款式分布（近 30 天成交额）</h2>
        <ul className="bars">
          {byStyle.map((s) => (
            <li key={s.style}>
              <span className="bars__label">{s.style}</span>
              <span className="bars__track">
                <span className="bars__fill" style={{ width: `${(s.gmv / maxStyle) * 100}%` }} />
              </span>
              <span className="bars__value">
                {compactMoney(s.gmv)}
                <em>转化 {ratio(s.cvr)}</em>
              </span>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
