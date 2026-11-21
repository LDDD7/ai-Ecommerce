"""诊断套件（T7 / ``diagnose``）的离线测试。

只跑 conftest 生成的 20 SKU / 30 天小库（表结构与真库一致，``anomaly_gt``
已被 ``rescale_anomalies`` 按比例缩放到这 30 天里），**不跑全量 25 条**：
``run()`` 只做 ``limit=1`` 冒烟，其余全部用纯函数 / 只读查询验证。

覆盖点：
- 用例数量、id 唯一、10 个 code 全覆盖、as_of 都落在库内日期且落在自己窗口内；
- ``score_case`` 的 top-1 / top-3 / miss 三种分支与「多真值」；
- 多真值时 precision 按集合算（重复预测去重）；
- ``run(limit=1)`` 的返回形状、``meta`` 字段、``detail`` 字段；
- ``run()`` 不写库（action_log / store_state 行数不变）。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app.eval.suites.diagnose_suite import CASE_COUNT, SUITE, cases, run, score_case

ALL_CODES = {f"R{i}" for i in range(1, 11)}


def _ro(db_path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)


def _db_bounds(db_path) -> tuple[date, date]:
    con = _ro(db_path)
    try:
        lo, hi = con.execute("SELECT MIN(dt), MAX(dt) FROM dim_date").fetchone()
    finally:
        con.close()
    return date.fromisoformat(lo), date.fromisoformat(hi)


def _table_counts(db_path) -> dict[str, int]:
    con = _ro(db_path)
    try:
        return {
            table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("action_log", "store_state")
        }
    finally:
        con.close()


# ---------------------------------------------------------------- 用例生成


def test_cases_returns_25_unique_ids(db_path):
    built = cases(db_path)
    assert len(built) == CASE_COUNT == 25
    ids = [c.case_id for c in built]
    assert len(set(ids)) == 25
    assert ids == [f"D{i:02d}" for i in range(1, 26)]
    assert all(c.suite == SUITE == "diagnose" for c in built)
    # 每条都带来自 anomaly_gt 的来源说明
    assert all(c.note and "anomaly_gt" in c.note for c in built)


def test_cases_cover_all_ten_codes(db_path):
    built = cases(db_path)
    covered = {code for case in built for code in case.payload["expect"]}
    assert covered == ALL_CODES
    for case in built:
        expect = case.payload["expect"]
        assert expect, f"{case.case_id} 的 expect 不能为空"
        assert set(expect) <= ALL_CODES
        assert len(expect) == len(set(expect))


def test_cases_as_of_within_db_dates(db_path):
    lo, hi = _db_bounds(db_path)
    for case in cases(db_path):
        as_of = date.fromisoformat(case.payload["as_of"])
        assert lo <= as_of <= hi


def test_cases_payload_shape_and_window_contains_as_of(db_path):
    for case in cases(db_path):
        payload = case.payload
        assert set(payload) == {"label", "as_of", "expect", "window"}
        assert isinstance(payload["label"], str) and payload["label"].strip()
        start, end = (date.fromisoformat(x) for x in payload["window"])
        assert start <= end
        # 顺延必须仍然落在自己的窗口内，否则这条用例的 expect 就不公平了
        assert start <= date.fromisoformat(payload["as_of"]) <= end
        # expect 必须包含「当天所有覆盖窗口的 code」，而不仅仅是自己
        assert len(payload["expect"]) >= 1


def test_cases_are_deterministic(db_path):
    first = [c.as_dict() for c in cases(db_path)]
    second = [c.as_dict() for c in cases(db_path)]
    assert first == second


def test_cases_expect_matches_gt_overlap(db_path):
    """expect = 当天所有窗口覆盖该日期的 code（区间包含，含端点）。"""
    con = _ro(db_path)
    try:
        rows = con.execute("SELECT code, dt_start, dt_end FROM anomaly_gt").fetchall()
    finally:
        con.close()

    windows = [
        (str(code).strip().upper(), date.fromisoformat(start[:10]), date.fromisoformat(end[:10]))
        for code, start, end in rows
    ]
    for case in cases(db_path):
        day = date.fromisoformat(case.payload["as_of"])
        want = sorted(
            {code for code, start, end in windows if start <= day <= end},
            key=lambda c: int(c[1:]),
        )
        assert case.payload["expect"] == want, case.case_id


# ---------------------------------------------------------------- 计分


def test_score_case_top1_hit():
    ok, score, detail = score_case(["R3", "R1", "R7"], ["R3"])
    assert ok is True
    assert score == 1.0
    assert detail["top1_hit"] is True
    assert detail["top3_hit"] is True
    assert detail["level"] == "top1"
    assert detail["precision"] == pytest.approx(1 / 3)
    assert detail["recall"] == 1.0
    assert detail["confusion"] == {"hit": 1, "extra": 2, "missed": 0}


def test_score_case_top3_hit_is_not_ok():
    ok, score, detail = score_case(["R5", "R3", "R8"], ["R3"])
    assert ok is False
    assert score == 0.6
    assert detail["top1_hit"] is False
    assert detail["top3_hit"] is True
    assert detail["level"] == "top3"
    assert detail["precision"] == pytest.approx(1 / 3)
    assert detail["recall"] == 1.0
    assert detail["confusion"] == {"hit": 1, "extra": 2, "missed": 0}


def test_score_case_miss():
    ok, score, detail = score_case(["R7", "R8"], ["R3"])
    assert ok is False
    assert score == 0.0
    assert detail["top1_hit"] is False
    assert detail["top3_hit"] is False
    assert detail["level"] == "miss"
    assert detail["precision"] == 0.0
    assert detail["recall"] == 0.0
    assert detail["confusion"] == {"hit": 0, "extra": 2, "missed": 1}


def test_score_case_empty_prediction():
    ok, score, detail = score_case([], ["R3"])
    assert (ok, score) == (False, 0.0)
    assert detail["top1_hit"] is False and detail["top3_hit"] is False
    assert detail["confusion"] == {"hit": 0, "extra": 0, "missed": 1}


def test_score_case_multiple_gold_top1():
    ok, score, detail = score_case(["R1", "R3", "R5"], ["R1", "R4"])
    assert (ok, score) == (True, 1.0)
    assert detail["top1_hit"] is True
    assert detail["precision"] == pytest.approx(1 / 3)
    assert detail["recall"] == pytest.approx(1 / 2)
    assert detail["confusion"] == {"hit": 1, "extra": 2, "missed": 1}


def test_score_case_multiple_gold_top3():
    ok, score, detail = score_case(["R9", "R4"], ["R1", "R4"])
    assert (ok, score) == (False, 0.6)
    assert detail["top1_hit"] is False and detail["top3_hit"] is True
    assert detail["precision"] == pytest.approx(1 / 2)
    assert detail["recall"] == pytest.approx(1 / 2)


def test_multicard_expect_precision_uses_sets(db_path):
    """真值有多个 code 时，precision = 命中的真值数 / 预测去重后的条数。"""
    multi = [c for c in cases(db_path) if len(c.payload["expect"]) > 1]
    assert multi, "小库里 R1/R10、R5/R9 的窗口应该重叠，必须存在多真值用例"

    case = multi[0]
    gold = case.payload["expect"]
    extra = next(code for code in sorted(ALL_CODES) if code not in set(gold))
    pred = [*gold, extra, extra]  # 重复预测应被去重
    ok, score, detail = score_case(pred, gold)

    assert (ok, score) == (True, 1.0)
    assert detail["precision"] == pytest.approx(len(gold) / (len(gold) + 1))
    assert detail["recall"] == 1.0
    assert detail["confusion"] == {"hit": len(gold), "extra": 1, "missed": 0}


# ---------------------------------------------------------------- 跑套件

META_KEYS = {"engine", "cases", "top1_rate", "top3_rate", "precision", "recall", "avg_cards", "avg_ms"}
DETAIL_KEYS = {
    "pred",
    "gold",
    "cards",
    "elapsed_ms",
    "precision",
    "recall",
    "top1_hit",
    "top3_hit",
    "confusion",
}


def test_run_limit_one_smoke(db_path):
    result = run(db_path, limit=1)
    assert result.suite == "diagnose"
    assert result.error is None
    assert result.total == 1
    assert result.elapsed_ms > 0
    assert result.cases[0].case_id == "D01"

    meta = result.meta
    assert META_KEYS <= set(meta)
    assert meta["engine"] == "rules-v1"
    assert meta["cases"] == 1
    assert 0.0 <= meta["top1_rate"] <= 1.0
    assert 0.0 <= meta["top3_rate"] <= 1.0
    assert meta["top3_rate"] >= meta["top1_rate"]
    assert 0.0 <= meta["precision"] <= 1.0
    assert 0.0 <= meta["recall"] <= 1.0
    assert meta["avg_cards"] >= 0.0
    assert meta["avg_ms"] > 0

    detail = result.cases[0].detail
    assert DETAIL_KEYS <= set(detail)
    assert detail["gold"] == cases(db_path)[0].payload["expect"]
    assert detail["as_of"] == cases(db_path)[0].payload["as_of"]
    assert isinstance(detail["pred"], list)
    assert isinstance(detail["cards"], list)
    assert detail["elapsed_ms"] > 0
    for card in detail["cards"]:
        assert set(card) == {"code", "title", "confidence", "gmv_impact"}
        assert str(card["code"]).startswith("R")

    ok, score, expected_detail = score_case(detail["pred"], detail["gold"])
    assert (result.cases[0].ok, result.cases[0].score) == (ok, score)
    assert expected_detail["confusion"] == detail["confusion"]


def test_run_limit_zero(db_path):
    result = run(db_path, limit=0)
    assert result.total == 0
    assert result.meta["cases"] == 0
    assert result.meta["top1_rate"] == 0.0
    assert result.meta["avg_cards"] == 0.0


def test_run_does_not_mutate_db(db_path):
    before = _table_counts(db_path)
    result = run(db_path, limit=1)
    after = _table_counts(db_path)

    assert result.total == 1
    assert after == before
    assert after["action_log"] == before["action_log"]
    assert after["store_state"] == before["store_state"]
