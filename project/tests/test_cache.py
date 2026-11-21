"""TTL 缓存与 single-flight 的行为测试（不依赖网络与数据库）。"""

from __future__ import annotations

import threading
import time

from app.core.cache import TTLCache, get_cache, reset_cache


def test_set_get_and_stats() -> None:
    c = TTLCache(ttl_s=30)
    c.set(("k",), {"v": 1})
    assert c.get(("k",)) == {"v": 1}
    stats = c.stats()
    assert stats["hits"] == 1
    assert stats["entries"] == 1


def test_ttl_expiry() -> None:
    c = TTLCache(ttl_s=0.05)
    c.set(("k",), 1)
    assert c.get(("k",)) == 1
    time.sleep(0.08)
    assert c.get(("k",)) is None


def test_get_or_compute_computes_once() -> None:
    c = TTLCache(ttl_s=30)
    calls: list[int] = []

    def factory() -> dict[str, int]:
        calls.append(1)
        return {"n": 1}

    value, cached = c.get_or_compute(("k",), factory)
    assert value == {"n": 1} and cached is False
    again, cached2 = c.get_or_compute(("k",), factory)
    assert again == value and cached2 is True
    assert len(calls) == 1


def test_single_flight_under_concurrency() -> None:
    """20 个线程同时打同一个键，只能真算一次（这是压测 P95 从 17s 降到 2.8s 的关键）。"""
    c = TTLCache(ttl_s=30)
    calls: list[int] = []

    def factory() -> str:
        calls.append(1)
        time.sleep(0.15)
        return "v"

    results: list[tuple[str, bool]] = []
    threads = [
        threading.Thread(target=lambda: results.append(c.get_or_compute(("same",), factory)))
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert all(v == "v" for v, _ in results)
    assert sum(1 for _, hit in results if hit) == 19


def test_factory_error_is_not_cached() -> None:
    c = TTLCache(ttl_s=30)

    def boom() -> int:
        raise RuntimeError("算不出来")

    for _ in range(2):
        try:
            c.get_or_compute(("bad",), boom)
        except RuntimeError:
            pass
    assert c.get(("bad",)) is None


def test_clear_and_reset_cache() -> None:
    c = TTLCache(ttl_s=30)
    c.set(("k",), 1)
    c.clear()
    assert c.get(("k",)) is None
    get_cache().set(("k",), 2)
    reset_cache()
    assert get_cache().get(("k",)) is None
