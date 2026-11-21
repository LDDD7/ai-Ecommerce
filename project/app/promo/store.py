"""促销日历的存取与回写（spec §28）。

设计要点：

1. **日历是唯一真源**：`promo_calendar` 存商家自己声明的促销期；
   `rebuild_flags()` 把它展开写回 `dim_date.is_promo / promo_name`，
   这样语义层、查询维度（「是否促销」）、诊断与 Critic 都不必改。
2. **空日历不动存量**：演示库是生成器直接写好的 50 个促销日，
   如果此时按空日历重算会把它们全抹成 0，所以没有显式 `force` 时直接跳过。
3. **删除要清干净**：删掉最后一条日历后，用被删那段的范围 `clear_range()` 兜底，
   否则那几天的 `is_promo` 会一直挂着。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

KINDS: dict[str, str] = {
    "campaign": "平台大促",
    "store": "店铺活动",
    "other": "其它",
}

#: 单段最长 90 天：再长就不像「促销期」了，多半是填错。
MAX_DAYS = 90

#: 日历覆盖之外的日期不碰。
_SEASON_MONTHS = (9, 10, 11, 12, 1)

PROMO_DDL = """
CREATE TABLE IF NOT EXISTS promo_calendar(
    promo_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'campaign',
    start_dt TEXT NOT NULL,
    end_dt TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""

_INDEX_DDL = "CREATE INDEX IF NOT EXISTS ix_promo_span ON promo_calendar(start_dt, end_dt)"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: str | Path) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(PROMO_DDL)
        conn.execute(_INDEX_DDL)
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    try:
        start = date.fromisoformat(str(data["start_dt"]))
        end = date.fromisoformat(str(data["end_dt"]))
        data["days"] = (end - start).days + 1
    except (ValueError, KeyError):
        data["days"] = 0
    data["kind_label"] = KINDS.get(str(data.get("kind")), str(data.get("kind") or ""))
    return data


def list_periods(db_path: str | Path) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM promo_calendar ORDER BY start_dt, created_at"
        ).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def get_period(db_path: str | Path, promo_id: str) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM promo_calendar WHERE promo_id = ?", (promo_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row(row) if row else None


def calendar_span(db_path: str | Path) -> tuple[str | None, str | None]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT MIN(start_dt) AS a, MAX(end_dt) AS b FROM promo_calendar"
        ).fetchone()
    finally:
        conn.close()
    return (row["a"], row["b"]) if row and row["a"] else (None, None)


def _validate(name: str, start_dt: str, end_dt: str, kind: str) -> tuple[str, str, str]:
    label = (name or "").strip()
    if not label:
        raise ValueError("促销名称不能为空")
    if len(label) > 40:
        raise ValueError("促销名称最多 40 个字")
    if kind not in KINDS:
        raise ValueError(f"类型只能是 {' / '.join(KINDS)}")
    try:
        start = date.fromisoformat(str(start_dt))
        end = date.fromisoformat(str(end_dt))
    except ValueError as exc:
        raise ValueError("日期格式应为 2026-11-11") from exc
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"单段最多 {MAX_DAYS} 天，长促请拆成几段")
    if not (date(2000, 1, 1) <= start and end <= date(2100, 1, 1)):
        raise ValueError("日期超出合理范围")
    return label, start.isoformat(), end.isoformat()


def create_period(
    db_path: str | Path,
    *,
    name: str,
    start_dt: str,
    end_dt: str,
    kind: str = "campaign",
    note: str = "",
) -> dict[str, Any]:
    label, start, end = _validate(name, start_dt, end_dt, kind)
    promo_id = "pr_" + uuid.uuid4().hex[:10]
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO promo_calendar(promo_id, name, kind, start_dt, end_dt, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (promo_id, label, kind, start, end, (note or "").strip()[:200], _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_period(db_path, promo_id) or {}


def update_period(
    db_path: str | Path,
    promo_id: str,
    *,
    name: str | None = None,
    start_dt: str | None = None,
    end_dt: str | None = None,
    kind: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    current = get_period(db_path, promo_id)
    if not current:
        raise LookupError("没有这条促销日历")
    label, start, end = _validate(
        name if name is not None else current["name"],
        start_dt if start_dt is not None else current["start_dt"],
        end_dt if end_dt is not None else current["end_dt"],
        kind if kind is not None else current["kind"],
    )
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE promo_calendar SET name = ?, kind = ?, start_dt = ?, end_dt = ?, note = ?"
            " WHERE promo_id = ?",
            (
                label,
                kind if kind is not None else current["kind"],
                start,
                end,
                (note if note is not None else current["note"] or "").strip()[:200],
                promo_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_period(db_path, promo_id) or {}


def delete_period(db_path: str | Path, promo_id: str) -> dict[str, Any]:
    current = get_period(db_path, promo_id)
    if not current:
        raise LookupError("没有这条促销日历")
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM promo_calendar WHERE promo_id = ?", (promo_id,))
        conn.commit()
    finally:
        conn.close()
    # 删掉的如果是最后一段，日历会变空、rebuild 会保守跳过，这里直接清掉这段的标记
    if not list_periods(db_path):
        clear_range(db_path, current["start_dt"], current["end_dt"])
    return current


def _dates(start: date, end: date) -> list[str]:
    out: list[str] = []
    day = start
    while day <= end:
        out.append(day.isoformat())
        day = date.fromordinal(day.toordinal() + 1)
    return out


def _ensure_rows(conn: sqlite3.Connection, days: list[str]) -> None:
    for value in days:
        day = date.fromisoformat(value)
        iso_year, iso_week, _ = day.isocalendar()
        conn.execute(
            "INSERT OR IGNORE INTO dim_date(dt, dow, week, month, is_promo, promo_name, season)"
            " VALUES (?, ?, ?, ?, 0, NULL, ?)",
            (
                value,
                day.isoweekday(),
                f"{iso_year}-W{iso_week:02d}",
                day.strftime("%Y-%m"),
                "旺季" if day.month in _SEASON_MONTHS else "淡季",
            ),
        )


def clear_range(db_path: str | Path, start_dt: str, end_dt: str) -> int:
    """把一段日期上的促销标记清掉（删最后一条日历时兜底用）。"""
    days = _dates(date.fromisoformat(start_dt), date.fromisoformat(end_dt))
    conn = _connect(db_path)
    touched = 0
    try:
        _ensure_rows(conn, days)
        for value in days:
            cur = conn.execute(
                "UPDATE dim_date SET is_promo = 0, promo_name = NULL"
                " WHERE dt = ? AND is_promo = 1",
                (value,),
            )
            touched += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return touched


def rebuild_flags(
    db_path: str | Path,
    *,
    start: str | None = None,
    end: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """按日历重算 `dim_date.is_promo / promo_name`。

    - `start` / `end` 给定时只重算这一段（导入后只碰本次导入的日期）；
    - 日历为空且 `force=False` 时直接跳过，避免抹掉生成器写好的演示标记。
    """
    periods = list_periods(db_path)
    cal_start, cal_end = calendar_span(db_path)

    if not periods and not force:
        return {
            "rebuilt": False,
            "reason": "促销日历为空，保留现有标记",
            "days": 0,
            "promo_days": 0,
            "periods": 0,
        }

    span_start = start or cal_start
    span_end = end or cal_end
    if not span_start or not span_end:
        return {"rebuilt": False, "reason": "没有可重算的范围", "days": 0, "promo_days": 0, "periods": len(periods)}

    first = date.fromisoformat(span_start)
    last = date.fromisoformat(span_end)
    if last < first:
        first, last = last, first

    # 日历 → 日期 → 名称（同一天被多段覆盖时取先开始的那段）
    marked: dict[str, str] = {}
    for period in periods:
        p_start = date.fromisoformat(period["start_dt"])
        p_end = date.fromisoformat(period["end_dt"])
        for value in _dates(max(p_start, first), min(p_end, last)):
            marked.setdefault(value, period["name"])

    days = _dates(first, last)
    conn = _connect(db_path)
    promo_days = 0
    try:
        _ensure_rows(conn, days)
        for value in days:
            name = marked.get(value)
            if name:
                conn.execute(
                    "UPDATE dim_date SET is_promo = 1, promo_name = ? WHERE dt = ?",
                    (name, value),
                )
                promo_days += 1
            else:
                conn.execute(
                    "UPDATE dim_date SET is_promo = 0, promo_name = NULL WHERE dt = ?",
                    (value,),
                )
        conn.commit()
    finally:
        conn.close()

    return {
        "rebuilt": True,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "days": len(days),
        "promo_days": promo_days,
        "periods": len(periods),
    }


def adopt_existing(db_path: str | Path) -> list[dict[str, Any]]:
    """把 `dim_date` 里已有的促销标记收编成日历（老库/演示库迁移用）。

    连续日期 + 同一 `promo_name` 合成一段；已经存在的同名同起止不会重复插。
    """
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT dt, promo_name FROM dim_date WHERE is_promo = 1 ORDER BY dt"
        ).fetchall()
    finally:
        conn.close()

    groups: list[tuple[str, date, date]] = []
    for row in rows:
        try:
            day = date.fromisoformat(str(row["dt"]))
        except ValueError:
            continue
        name = (row["promo_name"] or "促销").strip() or "促销"
        if groups and groups[-1][0] == name and (day - groups[-1][2]).days == 1:
            groups[-1] = (name, groups[-1][1], day)
        else:
            groups.append((name, day, day))

    existing = {
        (p["name"], p["start_dt"], p["end_dt"]) for p in list_periods(db_path)
    }
    created: list[dict[str, Any]] = []
    for name, first, last in groups:
        if (name, first.isoformat(), last.isoformat()) in existing:
            continue
        created.append(
            create_period(
                db_path,
                name=name,
                start_dt=first.isoformat(),
                end_dt=last.isoformat(),
                kind="campaign",
                note="从已有促销标记收编",
            )
        )
    return created


def summary(db_path: str | Path) -> dict[str, Any]:
    periods = list_periods(db_path)
    start, end = calendar_span(db_path)
    conn = _connect(db_path)
    try:
        promo_days = conn.execute(
            "SELECT COUNT(*) FROM dim_date WHERE is_promo = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "periods": periods,
        "count": len(periods),
        "range": {"start": start, "end": end},
        "promo_days": int(promo_days or 0),
        "max_days": MAX_DAYS,
        "kinds": KINDS,
    }
