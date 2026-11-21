"""指标目录与查询契约（QuerySpec）。

Agent 不写 SQL：它只输出 QuerySpec，由 compiler 生成本参数化 SELECT。
这一层负责「能查什么」的白名单校验，越界直接报错。
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.metrics.views import TIME_DIMS, VIEW_COLUMNS, VIEW_MEASURES, VIEWS

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")
MAX_METRICS = 6
MAX_DIMS = 3
MAX_FILTERS = 8
MAX_ROWS = 5000
MAX_DAYS = 400
MAX_IN_LEN = 50

SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "null", "case", "when", "then",
    "else", "end", "as", "distinct", "sum", "count", "avg", "min", "max", "cast",
    "real", "integer", "nullif", "coalesce", "in", "between", "like", "group",
    "order", "by", "limit", "desc", "asc", "is", "abs", "round",
}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class MetricError(ValueError):
    """QuerySpec 不合法（未知指标/维度、超限、注入嫌疑）。"""


class Filter(BaseModel):
    # 严格模式：多写的字段一律报错，而不是悄悄丢掉（否则像 dims/dimensions 这种笔误
    # 会得到「200 + 没分组」的假成功，调用方以为查对了）
    model_config = ConfigDict(extra="forbid")

    field: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "between", "like"]
    value: Any

    @field_validator("field")
    @classmethod
    def _field_ok(cls, v: str) -> str:
        if not _IDENT.fullmatch(v):
            raise ValueError(f"非法字段名: {v!r}")
        return v

    @model_validator(mode="after")
    def _value_shape(self) -> "Filter":
        if self.op in ("in", "not in"):
            if not isinstance(self.value, (list, tuple)) or not self.value:
                raise ValueError(f"{self.op} 需要非空列表")
            if len(self.value) > MAX_IN_LEN:
                raise ValueError(f"{self.op} 列表过长（>{MAX_IN_LEN}）")
        elif self.op == "between":
            if not isinstance(self.value, (list, tuple)) or len(self.value) != 2:
                raise ValueError("between 需要 [下界, 上界]")
        elif self.op == "like":
            if not isinstance(self.value, str) or len(self.value) > 64:
                raise ValueError("like 只接受 ≤64 字符字符串")
            if "%" not in self.value and "_" not in self.value:
                raise ValueError("like 模式需含 % 或 _")
        elif isinstance(self.value, (list, tuple, dict)):
            raise ValueError(f"{self.op} 只接受标量")
        return self


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date

    @model_validator(mode="after")
    def _span(self) -> "TimeRange":
        if self.end < self.start:
            raise ValueError("end 早于 start")
        if (self.end - self.start).days > MAX_DAYS:
            raise ValueError(f"时间跨度超过 {MAX_DAYS} 天")
        return self


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    dir: Literal["asc", "desc"] = "desc"


class QuerySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: list[str] = Field(min_length=1, max_length=MAX_METRICS)
    dimensions: list[str] = Field(default_factory=list, max_length=MAX_DIMS)
    time: TimeRange | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=MAX_FILTERS)
    order_by: list[OrderBy] = Field(default_factory=list, max_length=2)
    limit: int = Field(default=100, ge=1, le=MAX_ROWS)

    @field_validator("metrics", "dimensions")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for x in v:
            if not _IDENT.fullmatch(x):
                raise ValueError(f"非法标识符: {x!r}")
            if x not in out:
                out.append(x)
        return out

    @model_validator(mode="after")
    def _order_fields_exist(self) -> "QuerySpec":
        fields = set(self.metrics) | set(self.dimensions)
        for ob in self.order_by:
            if ob.field not in fields:
                raise ValueError(f"order_by 字段 {ob.field!r} 不在 metrics/dimensions 中")
        return self


class MetricDef(BaseModel):
    name: str
    display: str
    source: str
    expr: str
    unit: str = "count"
    dims: list[str] = Field(default_factory=list)
    description: str = ""

    # 由 loader 展开后填充
    allowed_dims: set[str] = Field(default_factory=set, exclude=True)


class Catalog:
    def __init__(self, metrics: dict[str, MetricDef], version: int, owner: str):
        self.metrics = metrics
        self.version = version
        self.owner = owner

    def get(self, name: str) -> MetricDef:
        try:
            return self.metrics[name]
        except KeyError:
            known = ", ".join(sorted(self.metrics))
            raise MetricError(f"未知指标 {name!r}；可用：{known}") from None

    def names(self) -> list[str]:
        return sorted(self.metrics)

    def public(self) -> list[dict]:
        return [
            {
                "name": m.name,
                "display": m.display,
                "unit": m.unit,
                "description": m.description,
                "dimensions": sorted(m.allowed_dims),
                "source": m.source,
            }
            for m in self.metrics.values()
        ]


def _check_expr(expr: str, source: str) -> None:
    """粗粒度校验 expr：只允许视图列、数字、运算符、白名单函数。"""
    allowed_cols = VIEW_COLUMNS[source] | VIEW_MEASURES[source]
    # 先去掉字符串字面量（'SIZE_SMALL' 之类），只审标识符
    stripped = re.sub(r"'(?:[^']|'')*'", "''", expr)
    for tok in _IDENT.findall(stripped):
        low = tok.lower()
        if low in SQL_KEYWORDS:
            continue
        if tok in allowed_cols or tok in TIME_DIMS:
            continue
        raise MetricError(f"指标表达式 {expr!r} 引用了未声明的列 {tok!r}")


@lru_cache(maxsize=1)
def load_catalog(path: str | None = None) -> Catalog:
    raw = yaml.safe_load(Path(path or CATALOG_PATH).read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = raw.get("dim_groups", {})
    metrics: dict[str, MetricDef] = {}
    for item in raw.get("metrics", []):
        m = MetricDef.model_validate(item)
        if m.source not in VIEWS:
            raise MetricError(f"指标 {m.name} 的 source {m.source!r} 不存在")
        _check_expr(m.expr, m.source)

        allowed: set[str] = set()
        for g in m.dims:
            if g in groups:
                allowed |= set(groups[g])
            elif g in TIME_DIMS or g in VIEW_COLUMNS[m.source]:
                allowed.add(g)
            else:
                raise MetricError(f"指标 {m.name} 引用了未知维度组 {g!r}")
        allowed |= {"dt", "week", "month"}
        allowed &= set(TIME_DIMS) | VIEW_COLUMNS[m.source]
        if not allowed:
            raise MetricError(f"指标 {m.name} 没有任何可用维度")
        m.allowed_dims = allowed
        if m.name in metrics:
            raise MetricError(f"指标重名: {m.name}")
        metrics[m.name] = m
    if not metrics:
        raise MetricError("指标目录为空")
    return Catalog(metrics, int(raw.get("version", 1)), str(raw.get("owner", "")))
