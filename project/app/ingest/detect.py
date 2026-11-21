"""列名识别：把一份来历不明的表，映射到规范字段。

三级证据，命中即停：
1. 列名与别名完全一致（归一后）—— 置信度 0.95；
2. 列名包含别名（或反之）—— 0.80；
3. 只看内容形态（日期 / 金额 / 整数 / 订单号 / SKU）—— 0.55~0.70。

识别不出的列不会被猜着用；缺必需字段时宁可拒绝，也不拿错的列硬算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ingest.canonical import (
    DATASETS,
    Dataset,
    Field,
    coerce,
    normalize_header,
    parse_date,
    parse_int,
    parse_money,
)
from app.ingest.loaders import Table

EXACT, CONTAINS, HEURISTIC = 0.95, 0.80, 0.60


@dataclass(slots=True)
class ColumnGuess:
    column: str
    field: str | None = None
    label: str | None = None
    kind: str | None = None
    confidence: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class Detection:
    dataset: str
    columns: list[ColumnGuess]
    mapping: dict[str, str]
    missing: list[str]
    header_row: int
    meta: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "mapping": self.mapping,
            "missing": self.missing,
            "header_row": self.header_row,
            "columns": [
                {
                    "column": c.column,
                    "field": c.field,
                    "label": c.label,
                    "kind": c.kind,
                    "confidence": round(c.confidence, 2),
                    "reason": c.reason,
                }
                for c in self.columns
            ],
            "meta": self.meta,
        }


def _values(table: Table, column: str, limit: int = 40) -> list[str]:
    index = table.headers.index(column)
    out: list[str] = []
    for row in table.rows[:limit]:
        if index < len(row):
            value = str(row[index]).strip()
            if value:
                out.append(value)
    return out


def _content_kind(values: list[str]) -> tuple[str, float]:
    """仅凭取值猜类型，返回 (kind, 命中率)。"""
    if not values:
        return ("", 0.0)
    total = len(values)
    dates = sum(1 for v in values if parse_date(v))
    moneys = sum(1 for v in values if parse_money(v) is not None)
    ints = sum(1 for v in values if parse_int(v) is not None and (parse_money(v) or 0) == int(parse_money(v) or 0))
    if dates / total >= 0.8:
        return ("date", dates / total)
    moneyish = sum(1 for v in values if any(ch in v for ch in "¥￥") or ("." in v and parse_money(v) is not None))
    if moneyish / total >= 0.5:
        return ("money", moneyish / total)
    if ints / total >= 0.9:
        return ("int", ints / total)
    return ("", 0.0)


def _alias_hit(field: Field, header: str) -> tuple[float, str]:
    target = normalize_header(header)
    if not target:
        return (0.0, "")
    for alias in field.aliases:
        if normalize_header(alias) == target:
            return (EXACT, f"列名与「{alias}」完全一致")
    for alias in field.aliases:
        needle = normalize_header(alias)
        if len(needle) >= 2 and (needle in target or target in needle):
            return (CONTAINS, f"列名包含「{alias}」")
    return (0.0, "")


def _heuristic_hit(field: Field, kind: str, ratio: float) -> tuple[float, str]:
    if kind and field.kind == kind:
        return (HEURISTIC + 0.1 * ratio, f"取值形态像{field.kind}")
    if field.name in {"order_id", "sku_id"} and kind == "":
        return (0.0, "")
    return (0.0, "")


def detect(table: Table, dataset: str | None = None) -> Detection:
    """识别表头并给出映射建议；`dataset` 未给时自动判断是哪类数据。"""
    headers = [h for h in table.headers if str(h).strip()]
    sample_kinds = {h: _content_kind(_values(table, h)) for h in headers}

    candidates: list[Detection] = []
    for name, spec in DATASETS.items():
        if dataset and name != dataset:
            continue
        candidates.append(_detect_one(table, spec, headers, sample_kinds))
    if not candidates:
        raise ValueError(f"未知数据集：{dataset}")

    # 先看「必需字段是否齐、有没有可用信号」，再看字段覆盖率：退款表和订单表都能凑出
    # 对方的必需字段，靠覆盖率区分（退款表能对上退款单号/金额/原因，字段更全）。
    def rank(d: Detection) -> tuple[int, int, int]:
        required = [f.name for f in DATASETS[d.dataset].fields if f.required]
        hit = sum(1 for r in required if r not in d.missing)
        signals = DATASETS[d.dataset].signals
        has_signal = (not signals) or any(name in d.mapping for name in signals)
        incomplete = 1 if (d.missing or not has_signal) else 0
        return (incomplete, -len(d.mapping), -hit)

    best = min(candidates, key=rank)
    best.meta["candidates"] = {d.dataset: len(d.mapping) for d in candidates}
    return best


def _detect_one(
    table: Table, spec: Dataset, headers: list[str], sample_kinds: dict[str, tuple[str, float]]
) -> Detection:
    columns = [ColumnGuess(column=h) for h in headers]
    taken: set[str] = set()
    mapping: dict[str, str] = {}

    # 先满足必需字段，再补可选字段：避免「金额」被一个边缘列抢走。
    ordered = sorted(spec.fields, key=lambda f: (not f.required, f.name))
    for wanted in ordered:
        best: tuple[float, str, int] | None = None
        for index, guess in enumerate(columns):
            if index in taken:
                continue
            score, reason = _alias_hit(wanted, guess.column)
            if score == 0.0:
                kind, ratio = sample_kinds.get(guess.column, ("", 0.0))
                score, reason = _heuristic_hit(wanted, kind, ratio)
            if score > 0.0 and (best is None or score > best[0]):
                best = (score, reason, index)
        if best is None:
            continue
        score, reason, index = best
        taken.add(index)
        guess = columns[index]
        guess.field, guess.label, guess.kind = wanted.name, wanted.label, wanted.kind
        guess.confidence, guess.reason = score, reason
        mapping[wanted.name] = guess.column

    missing = [f.label for f in spec.fields if f.required and f.name not in mapping]
    return Detection(
        dataset=spec.name,
        columns=columns,
        mapping=mapping,
        missing=missing,
        header_row=table.header_row,
        meta={
            "headers": headers,
            "rows": len(table.rows),
        },
    )


def apply_mapping(
    table: Table, dataset: str, mapping: dict[str, str]
) -> tuple[list[dict[str, str]], list[str]]:
    """按（可能被用户改过的）映射把表转成规范行；坏行跳过并记录前几条原因。"""
    spec = DATASETS[dataset]
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    for index, raw in enumerate(table.dict_rows(), start=1):
        row, problems = coerce(spec, mapping, raw)
        if problems:
            if len(errors) < 10:
                errors.append(f"第 {index} 行：" + "；".join(problems))
            continue
        rows.append(row)
    return rows, errors
