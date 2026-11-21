import { useCallback, useEffect, useState } from "react";

import {
  createPromo,
  deletePromo,
  getPromo,
  type PromoKind,
  type PromoPeriod,
  type PromoSummary,
  type PromoSync,
} from "../api-promo";

const KIND_OPTIONS: { value: PromoKind; label: string }[] = [
  { value: "campaign", label: "平台大促" },
  { value: "store", label: "店铺活动" },
  { value: "other", label: "其它" },
];

function syncText(sync: PromoSync | null): string {
  if (!sync) return "";
  if (!sync.rebuilt) return sync.reason ? `未重算：${sync.reason}` : "未重算";
  const days = sync.days ?? 0;
  const promoDays = sync.promo_days ?? 0;
  return `已重算 ${days} 天，其中 ${promoDays} 天标为促销`;
}

function errText(err: unknown): string {
  if (err instanceof Error) return err.message;
  return "操作失败，请稍后再试";
}

export function PromoCalendar() {
  const [summary, setSummary] = useState<PromoSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const [name, setName] = useState("");
  const [kind, setKind] = useState<PromoKind>("campaign");
  const [startDt, setStartDt] = useState("");
  const [endDt, setEndDt] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await getPromo();
      setSummary(data);
      setError("");
    } catch (err) {
      setError(errText(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = useCallback(async () => {
    if (!name.trim() || !startDt || !endDt) {
      setError("名称、开始日期、结束日期都要填");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await createPromo({ name: name.trim(), start_dt: startDt, end_dt: endDt, kind });
      setNote(`已新增「${res.period.name}」：${syncText(res.sync)}`);
      setName("");
      setStartDt("");
      setEndDt("");
      await load();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }, [name, startDt, endDt, kind, load]);

  const remove = useCallback(
    async (period: PromoPeriod) => {
      const ok = window.confirm(
        `删除「${period.name}」（${period.start_dt} ~ ${period.end_dt}）？这几天会变回非促销日。`,
      );
      if (!ok) return;
      setBusy(true);
      setError("");
      try {
        const res = await deletePromo(period.promo_id);
        setNote(`已删除「${res.removed.name}」：${syncText(res.sync)}`);
        await load();
      } catch (err) {
        setError(errText(err));
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  const periods = summary?.periods ?? [];
  const range = summary?.range;

  return (
    <section className="pc">
      <div className="pc__head">
        <div>
          <h2 className="pc__title">促销日历</h2>
          <p className="pc__desc">
            这里填的日期会被当作促销日：影响「促销日占比」、R7 大促透支能否被识别、以及量价对比是否可信。
            导入的真实订单本身不带促销标记，不填这一段，系统只会把它当成普通的一天。
          </p>
        </div>
        <div className="pc__stat">
          <b>{summary?.count ?? 0}</b> 段 · 已标记 <b>{summary?.promo_days ?? 0}</b> 天促销
          {range?.start && range?.end ? (
            <span className="pc__range">
              覆盖 {range.start} ~ {range.end}
            </span>
          ) : null}
        </div>
      </div>

      {loading ? <p className="pc__hint">正在读取…</p> : null}

      {periods.length > 0 ? (
        <div className="pc__tablewrap">
          <table className="pc__table">
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th>起止</th>
                <th>天数</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {periods.map((p) => (
                <tr key={p.promo_id}>
                  <td>
                    <b>{p.name}</b>
                    {p.note ? <span className="pc__note">{p.note}</span> : null}
                  </td>
                  <td>{p.kind_label}</td>
                  <td className="pc__dates">
                    {p.start_dt} ~ {p.end_dt}
                  </td>
                  <td className="pc__num">{p.days}</td>
                  <td>
                    <button
                      type="button"
                      className="pc__btn"
                      onClick={() => void remove(p)}
                      disabled={busy}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !loading && <p className="pc__hint">还没有促销期。先加一段，比如「双 11 · 10-20 ~ 11-11」。</p>
      )}

      <div className="pc__add">
        <label className="pc__field">
          <span>名称</span>
          <input
            value={name}
            maxLength={40}
            placeholder="双 11 / 店庆 / 黑五"
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="pc__field">
          <span>类型</span>
          <select value={kind} onChange={(e) => setKind(e.target.value as PromoKind)}>
            {KIND_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="pc__field">
          <span>开始</span>
          <input type="date" value={startDt} onChange={(e) => setStartDt(e.target.value)} />
        </label>
        <label className="pc__field">
          <span>结束</span>
          <input type="date" value={endDt} onChange={(e) => setEndDt(e.target.value)} />
        </label>
        <button type="button" className="btn pc__submit" onClick={() => void submit()} disabled={busy}>
          添加促销期
        </button>
      </div>

      {error && <p className="pc__error">{error}</p>}
      {note && !error && <p className="pc__note-line">{note}</p>}

      <p className="pc__hint pc__hint--foot">
        单段最多 {summary?.max_days ?? 90} 天（长促请拆成几段）；保存后立即生效，不用重启。
        修改或删除后会自动重算受影响的日期。
      </p>
    </section>
  );
}
