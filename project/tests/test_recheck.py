"""``app.diagnose.recheck`` 的单元 + 冒烟测试。

定位：这是只读数据层，判定逻辑不在它身上，所以用例分两层：
1. 用 monkeypatch 替换 ``app.diagnose.recheck.detect``，喂自造的异常点，
   钉住 scope 匹配、required_found、metrics 去重排序、points 排序/截断、
   as_of 归一化与 cache 复用——全部离线、毫秒级；
2. 用 conftest 的 20 SKU / 30 天小库跑真实 detect，验证「真数据上也能工作」。

注意：只有 ``detect`` 被替换（契约明确允许），``recheck_scope`` 自己的过滤与
聚合逻辑必须真跑。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.diagnose import recheck as recheck_mod
from app.diagnose.anomaly import DetectConfig, detect, resolve_as_of
from app.diagnose.recheck import (
    DEFAULT_RECHECK_DAYS,
    ScopeRecheck,
    recheck_many,
    recheck_scope,
)

SKU1 = ("sku_id", "SKU0001")
SKU2 = ("sku_id", "SKU0002")
STORE = ("all", "全店")
AS_OF = "2026-11-18"


# ---------------------------------------------------------------- 测试替身


class _Point:
    """``AnomalyPoint`` 的最小替身。

    同时提供属性访问与 ``as_dict()``，这样即使实现改用 ``point.as_dict()``
    取字段，用例依然成立——替身只负责喂数据，不替被测逻辑做判断。
    """

    def __init__(
        self,
        metric: str,
        *,
        score: float,
        dimension: str | None = "sku_id",
        dim_value: str | None = "SKU0001",
        current: float = 10.0,
        baseline: float = 8.0,
        delta_pct: float = 0.25,
        z: float = 3.0,
    ) -> None:
        self.metric = metric
        self.score = score
        self.dimension = dimension
        self.dim_value = dim_value
        self.current = current
        self.baseline = baseline
        self.delta_pct = delta_pct
        self.z = z

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "dimension": self.dimension,
            "dim_value": self.dim_value,
            "current": self.current,
            "baseline": self.baseline,
            "delta_pct": self.delta_pct,
            "z": self.z,
            "score": self.score,
        }

    def __repr__(self) -> str:  # 失败信息里能看出是哪个假点
        return f"<Point {self.metric} {self.dimension}={self.dim_value} score={self.score}>"


def _install_detect(monkeypatch, points: list[_Point]) -> list[dict]:
    """把 recheck 模块里的 detect 换成返回 fixed points，并记录调用参数。"""
    calls: list[dict] = []

    def fake_detect(db_path, *args, **kwargs):  # noqa: ANN001 - 对齐 detect 的签名
        calls.append({"db_path": db_path, "args": args, "kwargs": kwargs})
        return SimpleNamespace(points=list(points), as_of=None, window=None, baseline=None, meta={})

    monkeypatch.setattr(recheck_mod, "detect", fake_detect, raising=False)
    return calls


def _config_days(call: dict) -> int:
    """从一次 detect 调用里取出 ``DetectConfig.window_days``。"""
    cfg = call["kwargs"].get("config")
    if cfg is None:
        for arg in call["args"]:
            if isinstance(arg, DetectConfig):
                cfg = arg
                break
    assert isinstance(cfg, DetectConfig), (
        f"recheck 应当用 DetectConfig(window_days=days) 调 detect，"
        f"实际调用 args={call['args']} kwargs={call['kwargs']}"
    )
    return cfg.window_days


def _mixed_points() -> list[_Point]:
    return [
        _Point("roi", score=0.9, current=1.2, baseline=2.4, delta_pct=-0.5, z=-5.0),
        _Point("ctr", score=0.7, current=0.03, baseline=0.05, delta_pct=-0.4, z=-4.0),
        _Point("cvr", score=0.5, current=0.02, baseline=0.04, delta_pct=-0.5, z=-3.5),
        _Point("gmv", score=0.8, dim_value="SKU0002", current=800.0, baseline=1000.0, z=-3.0),
        _Point("ctr", score=0.6, dimension=None, dim_value=None, current=0.03, baseline=0.05, z=-6.0),
    ]


# ---------------------------------------------------------------- 单 scope


def test_scope命中_required_found_metrics_points(monkeypatch):
    """命中 scope 时：scope_found=True、必需辅证按 metrics 判定、points 按 score 降序。"""
    calls = _install_detect(monkeypatch, _mixed_points())

    r = recheck_scope("fake/store.db", SKU1, as_of=AS_OF, required_metric="roi")

    assert isinstance(r, ScopeRecheck)
    assert r.scope == SKU1
    assert r.scope_found is True, f"{SKU1} 在自造点里应命中，实际 scope_found=False"
    assert r.required_metric == "roi"
    assert r.required_found is True, f"metrics={r.metrics} 应包含 roi"
    assert r.metrics == ["ctr", "cvr", "roi"], f"metrics 应去重并按字母序，实际 {r.metrics}"
    assert [p["metric"] for p in r.points] == ["roi", "ctr", "cvr"], (
        f"points 应按 score 降序（0.9/0.7/0.5），实际 {[(p['metric'], p['score']) for p in r.points]}"
    )

    required_keys = {"metric", "current", "baseline", "delta_pct", "z", "score"}
    for p in r.points:
        assert required_keys <= set(p), f"point 缺字段：需要 {sorted(required_keys)}，实际 {sorted(p)}"
    roi = r.points[0]
    assert roi["current"] == pytest.approx(1.2)
    assert roi["baseline"] == pytest.approx(2.4)
    assert roi["delta_pct"] == pytest.approx(-0.5)
    assert roi["z"] == pytest.approx(-5.0)
    assert roi["score"] == pytest.approx(0.9)

    payload = r.as_dict()
    assert payload["scope"] == ["sku_id", "SKU0001"], "as_dict 的 scope 必须是 list"
    assert isinstance(payload["points"], list)
    assert payload["as_of"] == AS_OF
    assert payload["days"] == 14, f"默认窗口应为 14 天，实际 days={payload['days']}"
    assert DEFAULT_RECHECK_DAYS == 14, "契约常量 DEFAULT_RECHECK_DAYS 必须是 14"
    assert payload["elapsed_ms"] >= 0, "elapsed_ms 应存在且非负"
    json.dumps(payload, ensure_ascii=False)

    assert len(calls) == 1, f"一次 recheck 应只 detect 一次，实际 {len(calls)} 次"


def test_scope未命中_空结果(monkeypatch):
    """库里没有这个 scope：scope_found/required_found 为 False，metrics/points 为空。"""
    _install_detect(monkeypatch, _mixed_points())

    r = recheck_scope("fake/store.db", ("sku_id", "SKU_NOT_EXIST"), as_of=AS_OF, required_metric="roi")

    assert r.scope_found is False, "不存在的 scope 不应命中"
    assert r.required_found is False, "scope 未命中时 required_found 必须为 False"
    assert r.metrics == []
    assert r.points == []
    assert r.required_metric == "roi", "required_metric 应原样回填，便于调用方定位"


def test_points按score降序且最多8条(monkeypatch):
    """10 个异常点只留 score 最高的 8 个，顺序不能乱。"""
    scope = ("sku_id", "SKU_TRUNC")
    points = [_Point("roi", score=float(s), dim_value="SKU_TRUNC") for s in range(10, 0, -1)]
    _install_detect(monkeypatch, points)

    r = recheck_scope("fake/store.db", scope, as_of=AS_OF)

    assert r.scope_found is True
    assert len(r.points) == 8, f"最多 8 条，实际 {len(r.points)} 条"
    assert [p["score"] for p in r.points] == [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0], (
        f"应保留 score 最高的 8 条并降序，实际 {[p['score'] for p in r.points]}"
    )


def test_全店级scope匹配dimension为None的点(monkeypatch):
    """``("all","全店")`` 只看全店级点（dimension / dim_value 都是 None）。"""
    _install_detect(monkeypatch, _mixed_points())

    r = recheck_scope("fake/store.db", STORE, as_of=AS_OF)

    assert r.scope_found is True, "自造点里有一个全店级 ctr，应命中"
    assert r.metrics == ["ctr"], f"只应取全店级指标，实际 {r.metrics}"
    assert [p["metric"] for p in r.points] == ["ctr"], "不应把 sku 级点混进全店 scope"
    assert r.points[0]["score"] == pytest.approx(0.6)


def test_required_metric为None时required_found为False(monkeypatch):
    """没给必需辅证时不做「碰巧命中」，required_found 一律 False。"""
    _install_detect(monkeypatch, _mixed_points())

    r = recheck_scope("fake/store.db", SKU1, as_of=AS_OF)
    assert r.required_metric is None
    assert r.required_found is False, "required_metric=None 时 required_found 必须为 False"
    assert r.metrics == ["ctr", "cvr", "roi"], "metrics 仍应照常返回"

    r2 = recheck_scope("fake/store.db", SKU1, as_of=AS_OF, required_metric="stockout_rate")
    assert r2.required_metric == "stockout_rate"
    assert r2.required_found is False, f"stockout_rate 不在 {r2.metrics} 里，不应 required_found"


# ---------------------------------------------------------------- as_of / cache


def test_as_of_支持date与str且归一到同一天(monkeypatch):
    """date 与 ISO 字符串等价：as_of 归一成 YYYY-MM-DD，并共用同一个缓存键。"""
    calls = _install_detect(monkeypatch, _mixed_points())
    cache: dict = {}

    r_date = recheck_scope("fake/store.db", SKU1, as_of=date(2026, 11, 18), cache=cache)
    assert r_date.as_of == "2026-11-18", f"date 应归一成 ISO 字符串，实际 {r_date.as_of!r}"

    r_str = recheck_scope("fake/store.db", SKU1, as_of="2026-11-18", cache=cache)
    assert r_str.as_of == "2026-11-18"
    assert len(calls) == 1, (
        f"date 与同一天的字符串应命中同一个缓存键，detect 实际被调用 {len(calls)} 次"
    )


def test_cache_同键只detect一次(monkeypatch):
    """同一 (db_path, as_of, days) 第二次调用不再 detect；换 days 才重新算。"""
    calls = _install_detect(monkeypatch, _mixed_points())
    cache: dict = {}

    first = recheck_scope("fake/store.db", SKU1, as_of=AS_OF, days=14, cache=cache)
    second = recheck_scope("fake/store.db", SKU1, as_of=AS_OF, days=14, cache=cache)

    assert len(calls) == 1, f"同键两次调用应只 detect 一次，实际 {len(calls)} 次"
    assert cache, "传入 cache 后应写入缓存"
    resolved = str(Path("fake/store.db").resolve())
    assert (resolved, AS_OF, 14) in cache, (
        f"缓存键应为 (resolve 后的 db_path, as_of_iso, days)，实际键 {list(cache)}"
    )
    assert second.points == first.points

    recheck_scope("fake/store.db", SKU1, as_of=AS_OF, days=28, cache=cache)
    assert len(calls) == 2, f"days 变了应重新 detect，实际累计 {len(calls)} 次"
    assert _config_days(calls[0]) == 14, "days=14 时应以 window_days=14 调 detect"
    assert _config_days(calls[1]) == 28, "days=28 时应以 window_days=28 调 detect"


def test_不传cache时每次都detect(monkeypatch):
    """cache 是 opt-in：不传就不缓存（模块 docstring 的「测试友好」约定）。"""
    calls = _install_detect(monkeypatch, _mixed_points())

    recheck_scope("fake/store.db", SKU1, as_of=AS_OF)
    recheck_scope("fake/store.db", SKU1, as_of=AS_OF)

    assert len(calls) == 2, f"不传 cache 时应每次重算，detect 实际被调用 {len(calls)} 次"


# ---------------------------------------------------------------- 批量


def test_recheck_many_去重且键为tuple(monkeypatch):
    """重复 scope 只算一次；返回值键必须是 (dim, value) 元组；required 按 scope 生效。"""
    calls = _install_detect(monkeypatch, _mixed_points())
    cache: dict = {}

    out = recheck_many(
        "fake/store.db",
        [SKU1, SKU1, SKU2, SKU1],
        as_of=AS_OF,
        required={SKU1: "roi"},
        cache=cache,
    )

    assert isinstance(out, dict)
    assert set(out) == {SKU1, SKU2}, f"应去重成两个 scope，实际 {set(out)}"
    assert all(isinstance(k, tuple) and len(k) == 2 for k in out), f"键应为 (dim, value)，实际 {list(out)}"
    assert out[SKU1].required_found is True, f"SKU0001 的 metrics={out[SKU1].metrics} 应含 roi"
    assert out[SKU2].required_found is False, "SKU0002 没要求辅证，required_found 应为 False"
    assert out[SKU2].required_metric is None
    assert len(calls) == 1, f"同一 (db, as_of, days) 应复用 detect，实际 {len(calls)} 次"


def test_recheck_many_空输入(monkeypatch):
    """空列表直接返回 {}，不触发任何 detect。"""
    calls = _install_detect(monkeypatch, _mixed_points())

    out = recheck_many("fake/store.db", [], as_of=AS_OF)

    assert out == {}
    assert calls == [], f"空输入不应 detect，实际 {len(calls)} 次"


# ---------------------------------------------------------------- 真实小库冒烟


def test_real_db_命中真实scope(db_path):
    """真实小库：先用 detect 找一个真异常点，再用 recheck_scope 复检它。

    期望 scope_found=True、as_of 是 YYYY-MM-DD、points 不超过 8 条且按 score 降序。
    """
    as_of = resolve_as_of(db_path)
    assert isinstance(as_of, date)

    detection = detect(db_path, as_of=as_of, config=DetectConfig(window_days=14))
    assert detection.points, "20 SKU/30 天小库在 14 天窗口下应有异常点，冒烟测试才成立"

    point = next(p for p in detection.points if p.dimension is not None)
    scope = (point.dimension, point.dim_value)
    cache: dict = {}

    r = recheck_scope(
        str(db_path),
        scope,
        as_of=as_of,
        required_metric=point.metric,
        days=14,
        cache=cache,
    )

    assert r.scope == scope
    assert r.scope_found is True, f"真实异常点 {scope} 复检应命中，实际 scope_found=False"
    assert r.as_of == as_of.isoformat(), f"as_of 应为 YYYY-MM-DD，实际 {r.as_of!r}"
    assert r.days == 14
    assert r.required_metric == point.metric
    assert r.required_found is True, f"{point.metric} 就在该 scope 的点里，metrics={r.metrics}"
    assert 1 <= len(r.points) <= 8, f"points 应为 1..8 条，实际 {len(r.points)}"
    scores = [p["score"] for p in r.points]
    assert scores == sorted(scores, reverse=True), f"points 应按 score 降序，实际 {scores}"
    json.dumps(r.as_dict(), ensure_ascii=False)


def test_real_db_全店级与不存在的scope(db_path):
    """真实小库：("all","全店") 应命中全店级异常；不存在的 scope 必须返回空。"""
    as_of = resolve_as_of(db_path)
    detection = detect(db_path, as_of=as_of, config=DetectConfig(window_days=14))
    assert any(p.dimension is None for p in detection.points), (
        "小库在 14 天窗口下应有全店级异常点（否则全店 scope 冒烟无意义）"
    )
    cache: dict = {}

    store = recheck_scope(str(db_path), STORE, as_of=as_of, days=14, cache=cache)
    assert store.scope_found is True, f"全店级应命中，metrics={store.metrics}"
    assert store.metrics, "命中全店级时 metrics 不应为空"
    assert all(p["metric"] in store.metrics for p in store.points)

    missing = recheck_scope(str(db_path), ("sku_id", "SKU_NOT_EXIST"), as_of=as_of, days=14, cache=cache)
    assert missing.scope_found is False, "不存在的 SKU 不应命中"
    assert missing.points == []
    assert missing.metrics == []
    assert missing.required_found is False
