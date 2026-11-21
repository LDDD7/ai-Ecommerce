/** 展示用格式化：金额千分位、比率转百分比、涨跌符号。 */

export function money(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `¥${v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function compactMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e8) return `¥${(v / 1e8).toFixed(2)} 亿`;
  // 千万级不再带小数，否则「¥3695.2 万」在窄卡片里会顶破边框
  if (abs >= 1e7) return `¥${(v / 1e4).toFixed(0)} 万`;
  if (abs >= 1e4) return `¥${(v / 1e4).toFixed(1)} 万`;
  return money(v);
}

export function count(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function ratio(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function byUnit(v: number | null | undefined, unit: string | undefined): string {
  if (unit === "ratio") return ratio(v, 2);
  if (unit === "currency") return money(v, 0);
  return count(v, Number.isInteger(v) ? 0 : 1);
}

/** 涨跌方向 → 语义色：坏为朱红，好为苔绿，持平为灰。 */
export function tone(deltaPct: number | null | undefined, goodWhenDown = false): "up" | "down" | "flat" {
  if (deltaPct === null || deltaPct === undefined || Math.abs(deltaPct) < 0.05) return "flat";
  const up = deltaPct > 0;
  const isGood = goodWhenDown ? !up : up;
  return isGood ? "up" : "down";
}
