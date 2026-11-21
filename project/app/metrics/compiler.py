"""QuerySpec -> SQL 编译器 + 只读执行器。

安全边界
1. 不给 Agent 写 SQL 的口子：只接受 QuerySpec（指标/维度/过滤），SQL 全由这里拼。
2. 所有值走参数绑定；标识符只能来自目录白名单。
3. 连接用 `mode=ro`（物理只读），并有 step 级超时。
4. 行数硬上限、时间跨度上限、指标/维度数量上限。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.metrics.spec import (
    MAX_ROWS,
    Catalog,
    MetricError,
    QuerySpec,
    load_catalog,
)
from app.metrics.views import TIME_DIMS, VIEW_COLUMNS, VIEWS

DEFAULT_TIMEOUT_S = 8.0
HARD_ROW_CAP = 50_000
OP_SQL = {
    "=": "=",
    "!=": "!=",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "in": "IN",
    "not in": "NOT IN",
    "between": "BETWEEN",
    "like": "LIKE",
}


@dataclass
class CompiledQuery:
    source: str
    sql: str
    params: list[Any]
    metrics: list[str] = field(default_factory=list)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    meta: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows, "meta": self.meta}


def _dim_expr(dim: str) -> str:
    return TIME_DIMS.get(dim, f"v.{dim}")


def validate(spec: QuerySpec, catalog: Catalog | None = None) -> tuple[Catalog, dict[str, list[str]]]:
    """校验并返回 (catalog, {source: metrics})。"""
    cat = catalog or load_catalog()
    by_source: dict[str, list[str]] = {}
    allowed_sets: list[set[str]] = []
    for name in spec.metrics:
        m = cat.get(name)
        allowed_sets.append(m.allowed_dims)
        by_source.setdefault(m.source, []).append(name)

    common = set.intersection(*allowed_sets) if allowed_sets else set()
    for d in spec.dimensions:
        if d not in common:
            owners = [n for n in spec.metrics if d not in cat.get(n).allowed_dims]
            raise MetricError(f"维度 {d!r} 不被指标 {owners} 支持；可选 {sorted(common)}")
    for f in spec.filters:
        if f.field not in common:
            raise MetricError(f"过滤字段 {f.field!r} 不在可选维度内；可选 {sorted(common)}")
    return cat, by_source


def build(spec: QuerySpec, catalog: Catalog | None = None) -> list[CompiledQuery]:
    cat, by_source = validate(spec, catalog)
    dim_selects = [f"{_dim_expr(d)} AS {d}" for d in spec.dimensions]
    group_pos = [str(i + 1) for i in range(len(spec.dimensions))]

    where = ["v.dt BETWEEN ? AND ?"]
    params: list[Any] = []
    if spec.time:
        params += [spec.time.start.isoformat(), spec.time.end.isoformat()]
    else:
        params += ["1900-01-01", "2999-12-31"]

    for f in spec.filters:
        col = _dim_expr(f.field)
        op = OP_SQL[f.op]
        if f.op in ("in", "not in"):
            ph = ",".join("?" * len(f.value))
            where.append(f"{col} {op} ({ph})")
            params += list(f.value)
        elif f.op == "between":
            where.append(f"{col} BETWEEN ? AND ?")
            params += list(f.value)
        else:
            where.append(f"{col} {op} ?")
            params.append(f.value)

    queries: list[CompiledQuery] = []
    for source, names in by_source.items():
        if source not in VIEWS:
            raise MetricError(f"未知数据源 {source!r}")
        exprs = [f"{cat.get(n).expr} AS {n}" for n in names]
        select = ", ".join(dim_selects + exprs)
        sql = (
            f"SELECT {select}\n"
            f"FROM ({VIEWS[source]}) v\n"
            f"JOIN dim_date d ON d.dt = v.dt\n"
            f"WHERE {' AND '.join(where)}\n"
        )
        if group_pos:
            sql += f"GROUP BY {', '.join(group_pos)}\n"
        sql += f"LIMIT {HARD_ROW_CAP}"
        queries.append(CompiledQuery(source=source, sql=sql, params=list(params), metrics=names))
    return queries


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA query_only = ON")
    return con


def run(
    spec: QuerySpec,
    db_path: Path | str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    catalog: Catalog | None = None,
) -> QueryResult:
    t0 = time.perf_counter()
    cat = catalog or load_catalog()
    queries = build(spec, cat)
    path = Path(db_path)
    if not path.exists():
        raise MetricError(f"数据库不存在: {path}")

    deadline = time.monotonic() + timeout_s
    con = _connect_ro(path)

    def _watchdog() -> int:
        return 1 if time.monotonic() > deadline else 0

    con.set_progress_handler(_watchdog, 20_000)

    per_source: list[dict[tuple, dict[str, Any]]] = []
    row_counts: dict[str, int] = {}
    try:
        for q in queries:
            rows = con.execute(q.sql, q.params).fetchall()
            if len(rows) >= HARD_ROW_CAP:
                raise MetricError(f"结果集超过硬上限 {HARD_ROW_CAP} 行，请收窄时间或加维度过滤")
            row_counts[q.source] = len(rows)
            table: dict[tuple, dict[str, Any]] = {}
            for r in rows:
                key = tuple(r[: len(spec.dimensions)])
                table[key] = {**dict(zip(spec.dimensions, key)),
                              **dict(zip(q.metrics, r[len(spec.dimensions):]))}
            per_source.append(table)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise MetricError(f"查询超过 {timeout_s}s 被中断，请缩小范围") from None
        raise MetricError(f"查询失败: {e}") from None
    finally:
        con.close()

    merged: dict[tuple, dict[str, Any]] = {}
    for table in per_source:
        for key, row in table.items():
            merged.setdefault(key, {}).update(row)

    rows = list(merged.values())
    rows = _sort(rows, spec)

    columns = list(spec.dimensions) + list(spec.metrics)
    out_rows = [[_round(r.get(c)) for c in columns] for r in rows[: spec.limit]]
    elapsed = round((time.perf_counter() - t0) * 1000, 1)

    return QueryResult(
        columns=columns,
        rows=out_rows,
        meta={
            "elapsed_ms": elapsed,
            "row_count": len(out_rows),
            "total_groups": len(rows),
            "sources": {q.source: row_counts.get(q.source, 0) for q in queries},
            "metrics": [
                {"name": n, "display": cat.get(n).display, "unit": cat.get(n).unit}
                for n in spec.metrics
            ],
            "sql_statements": len(queries),
            "truncated": len(rows) > spec.limit,
            "max_rows": MAX_ROWS,
        },
    )


def _round(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 6)
    return v


def _sort(rows: list[dict[str, Any]], spec: QuerySpec) -> list[dict[str, Any]]:
    if not spec.order_by:
        return rows
    out = rows
    for ob in reversed(spec.order_by):
        def key(r: dict[str, Any], _f: str = ob.field):
            v = r.get(_f)
            return (v is None, v if v is not None else 0)

        try:
            out = sorted(out, key=key, reverse=(ob.dir == "desc"))
        except TypeError:
            raise MetricError(f"order_by 字段 {ob.field!r} 类型不可比较") from None
    return out
