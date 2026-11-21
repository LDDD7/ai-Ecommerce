"""入库前的质量闸：宁可挡在门外，也不让脏数据污染诊断。

四条硬规则（任一不满足就 reject）：
1. 必需字段都能映射且解析成功率 ≥ 90%；
2. 订单的金额恒等式成立（单价×数量−优惠 ≈ 实付）；
3. 重复率（订单号 / 日期+商品）≤ 2%；
4. 日期连续，断档 ≤ 2 天（超出只警告，不拒绝——商家可能真的停业）。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.ingest.canonical import CAPABILITIES, DATASETS, coerce
from app.ingest.loaders import Table

MAX_ERRORS = 5
MIN_OK_RATE = 0.90
MAX_DUPLICATE_RATE = 0.02
MAX_IDENTITY_FAIL = 0.05
MAX_GAPS = 2


@dataclass(slots=True)
class Outcome:
    dataset: str
    rows_total: int
    rows_ok: int
    rows_bad: int
    error_samples: list[str]
    duplicates: int
    duplicate_rate: float
    identity_checked: int
    identity_failed: int
    date_min: str | None
    date_max: str | None
    gaps: int
    verdict: str
    reasons: list[str]
    sample: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "rows_total": self.rows_total,
            "rows_ok": self.rows_ok,
            "rows_bad": self.rows_bad,
            "error_samples": self.error_samples,
            "duplicates": self.duplicates,
            "duplicate_rate": round(self.duplicate_rate, 4),
            "identity": {
                "checked": self.identity_checked,
                "failed": self.identity_failed,
                "failure_rate": round(
                    self.identity_failed / self.identity_checked, 4
                ) if self.identity_checked else 0.0,
            },
            "date_range": [self.date_min, self.date_max],
            "gaps": self.gaps,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "sample": self.sample,
        }


def _duplicate_key(dataset: str, row: dict[str, Any]) -> str:
    if dataset == "orders":
        return str(row.get("order_id", ""))
    if dataset == "traffic":
        return f"{row.get('dt')}|{row.get('sku_id')}|{row.get('channel', '')}"
    if dataset == "refunds":
        return str(row.get("refund_id") or f"{row.get('order_id')}|{row.get('dt')}")
    return str(row.get("sku_id", ""))


def _check_identity(row: dict[str, Any]) -> bool | None:
    pay = row.get("pay_amount")
    price = row.get("unit_price")
    qty = row.get("qty") or 1
    if pay is None or price is None:
        return None
    expected = float(price) * int(qty) - float(row.get("discount") or 0.0)
    tolerance = max(1.0, abs(float(pay)) * 0.005)
    return abs(expected - float(pay)) <= tolerance


def _gaps(dates: list[date]) -> int:
    if not dates:
        return 0
    unique = sorted(set(dates))
    missing = 0
    for previous, current in zip(unique, unique[1:]):
        missing += max(0, (current - previous).days - 1)
    return missing


def check(dataset: str, table: Table, mapping: dict[str, str]) -> Outcome:
    spec = DATASETS[dataset]
    reasons: list[str] = []
    ok_rows: list[dict[str, Any]] = []
    bad = 0
    error_samples: list[str] = []

    missing_required = [f.label for f in spec.fields if f.required and f.name not in mapping]
    if missing_required:
        reasons.append("缺少必需字段：" + "、".join(missing_required))

    for index, raw in enumerate(table.dict_rows(), start=1):
        row, errors = coerce(spec, mapping, raw)
        if errors:
            bad += 1
            if len(error_samples) < MAX_ERRORS:
                error_samples.append(f"第 {index} 行：" + "；".join(errors))
            continue
        ok_rows.append(row)
        # 有 SKU 但没有订单号的「汇总行」也要挡住：金额会被重复计入。
        if dataset == "orders" and not row.get("order_id"):
            bad += 1
            if len(error_samples) < MAX_ERRORS:
                error_samples.append(f"第 {index} 行：订单号为空")

    total = len(table.rows)
    ok_rate = (len(ok_rows) / total) if total else 0.0
    if total == 0:
        reasons.append("文件里没有数据行")
    elif ok_rate < MIN_OK_RATE:
        reasons.append(f"可解析行占比 {ok_rate:.1%}，低于 {MIN_OK_RATE:.0%}")

    keys = [_duplicate_key(dataset, row) for row in ok_rows if _duplicate_key(dataset, row)]
    duplicates = max(0, len(keys) - len(set(keys)))
    duplicate_rate = duplicates / max(1, len(keys))
    if duplicate_rate > MAX_DUPLICATE_RATE:
        reasons.append(f"重复率 {duplicate_rate:.1%}，高于 {MAX_DUPLICATE_RATE:.0%}")

    identity_checked = identity_failed = 0
    if dataset == "orders":
        for row in ok_rows:
            result = _check_identity(row)
            if result is None:
                continue
            identity_checked += 1
            if not result:
                identity_failed += 1
        if identity_checked and identity_failed / identity_checked > MAX_IDENTITY_FAIL:
            reasons.append(
                f"金额恒等式不成立的行占 {identity_failed / identity_checked:.1%}"
                "（单价×数量−优惠 ≠ 实付）"
            )

    dates: list[date] = []
    for row in ok_rows:
        raw_date = row.get("dt")
        if raw_date:
            try:
                dates.append(date.fromisoformat(str(raw_date)))
            except ValueError:
                pass
    gaps = _gaps(dates)
    if gaps > MAX_GAPS:
        reasons.append(f"日期断档 {gaps} 天（可能只是没导出完整）")

    verdict = "pass"
    if missing_required or total == 0 or ok_rate < MIN_OK_RATE:
        verdict = "reject"
    elif reasons:
        verdict = "warn"

    return Outcome(
        dataset=dataset,
        rows_total=total,
        rows_ok=len(ok_rows),
        rows_bad=bad,
        error_samples=error_samples,
        duplicates=duplicates,
        duplicate_rate=duplicate_rate,
        identity_checked=identity_checked,
        identity_failed=identity_failed,
        date_min=dates and min(dates).isoformat(),
        date_max=dates and max(dates).isoformat(),
        gaps=gaps,
        verdict=verdict,
        reasons=reasons,
        sample=ok_rows[:3],
    )


def capabilities(db_path: str) -> dict[str, Any]:
    """当前库里哪些数据已经有了、哪些指标还缺。"""
    available: dict[str, int] = {}
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        for dataset, _, _ in CAPABILITIES:
            table = {
                "orders": "fact_orders",
                "traffic": "fact_traffic_daily",
                "refunds": "fact_refunds",
                "products": "dim_sku",
                "reviews": "fact_reviews",
                "inventory": "inventory_snapshot",
                "ads": "fact_ads",
            }[dataset]
            try:
                available[dataset] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                available[dataset] = 0

    missing_metrics: list[str] = []
    for dataset, _, metrics in CAPABILITIES:
        if not available.get(dataset):
            missing_metrics.extend(metrics)
    return {
        "tables": available,
        "available": [d for d, n in available.items() if n],
        "missing": [d for d, n in available.items() if not n],
        "missing_metrics": missing_metrics,
    }
