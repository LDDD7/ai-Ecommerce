import { requestJson } from "./api";

export type PromoKind = "campaign" | "store" | "other";

export type PromoPeriod = {
  promo_id: string;
  name: string;
  kind: PromoKind;
  kind_label: string;
  start_dt: string;
  end_dt: string;
  days: number;
  note: string;
  created_at: string;
};

export type PromoSync = {
  rebuilt: boolean;
  reason?: string;
  start?: string;
  end?: string;
  days?: number;
  promo_days?: number;
  periods?: number;
  cleared?: number;
};

export type PromoSummary = {
  periods: PromoPeriod[];
  count: number;
  range: { start: string | null; end: string | null };
  promo_days: number;
  max_days: number;
  kinds: Record<PromoKind, string>;
};

export type PromoWriteResult = { period: PromoPeriod; sync: PromoSync };

export function getPromo(): Promise<PromoSummary> {
  return requestJson<PromoSummary>("/api/promo");
}

export function createPromo(payload: {
  name: string;
  start_dt: string;
  end_dt: string;
  kind: PromoKind;
  note?: string;
}): Promise<PromoWriteResult> {
  return requestJson<PromoWriteResult>("/api/promo", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updatePromo(
  promoId: string,
  payload: Partial<{ name: string; start_dt: string; end_dt: string; kind: PromoKind; note: string }>,
): Promise<PromoWriteResult> {
  return requestJson<PromoWriteResult>(`/api/promo/${encodeURIComponent(promoId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deletePromo(promoId: string): Promise<{ removed: PromoPeriod; sync: PromoSync }> {
  return requestJson<{ removed: PromoPeriod; sync: PromoSync }>(
    `/api/promo/${encodeURIComponent(promoId)}`,
    { method: "DELETE" },
  );
}

export function rebuildPromo(): Promise<{ sync: PromoSync }> {
  return requestJson<{ sync: PromoSync }>("/api/promo/rebuild", {
    method: "POST",
    body: JSON.stringify({}),
  });
}
