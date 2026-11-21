"""进程内 TTL 缓存（T10）。

只干一件事：把**同参数的重复重计算**折叠掉。压测里 20 并发打同一个 `as_of` 的诊断，
第一发 1.5s、其余 19 发直接命中，P95 从 17s 掉到百毫秒级——这不是「作弊」，而是真实
使用方式（用户刷新页面、多个浏览器 tab、演示时反复点）。

边界（都要守住）：

- 键里必须带**数据库 mtime**：数据重建后 mtime 变了，旧结果自动失效；
- 只缓存「纯读」结果（overview / diagnose），不缓存动作与设置；
- 容量与 TTL 都小（64 条 / 120s），内存不会涨；
- 线程安全（`threading.Lock`），因为 FastAPI 的同步路由跑在线程池里；
- 命中信息要透传给调用方（`meta.cached`），不假装每次都是新算的。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULT_TTL_S = 120.0
DEFAULT_MAXSIZE = 64


class TTLCache:
    def __init__(self, *, ttl_s: float = DEFAULT_TTL_S, maxsize: int = DEFAULT_MAXSIZE) -> None:
        self._ttl = ttl_s
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._locks_guard = threading.Lock()
        self._locks: dict[tuple, threading.Lock] = {}
        self._data: dict[tuple, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: tuple) -> Any | None:
        return self._lookup(key, count=True)

    def _lookup(self, key: tuple, *, count: bool) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                if count:
                    self.misses += 1
                return None
            expires_at, value = item
            if expires_at <= now:
                self._data.pop(key, None)
                if count:
                    self.misses += 1
                return None
            if count:
                self.hits += 1
            return value

    def set(self, key: tuple, value: Any) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._data) >= self._maxsize and key not in self._data:
                # 先清过期，再按「最早过期」淘汰一条
                expired = [k for k, (exp, _) in self._data.items() if exp <= now]
                for k in expired:
                    self._data.pop(k, None)
                if len(self._data) >= self._maxsize:
                    oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
                    self._data.pop(oldest, None)
                    self.evictions += 1
            self._data[key] = (now + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._data),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "ttl_s": self._ttl,
                "maxsize": self._maxsize,
            }

    def get_or_compute(self, key: tuple, factory: Callable[[], Any]) -> tuple[Any, bool]:
        """返回 (值, 是否命中)。`factory` 抛异常时不写缓存。

        同一键并发进来时**只算一次**（single-flight）：20 个请求打同一个 `as_of`，
        第一个算、其余在键锁上等，算完一起拿结果。没有这一步，20 个请求会各算一遍，
        缓存反而把「同时到达」的尖峰全放了进去（实测 P95 17s 就是这个原因）。
        """
        cached = self.get(key)
        if cached is not None:
            return cached, True
        with self._key_lock(key):
            cached = self._lookup(key, count=True)  # 等锁期间别人算完了 → 记为命中
            if cached is not None:
                return cached, True
            value = factory()
            self.set(key, value)
            return value, False

    def _key_lock(self, key: tuple) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                if len(self._locks) >= self._maxsize * 2:
                    self._locks.clear()
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


_cache = TTLCache()


def get_cache() -> TTLCache:
    return _cache


def reset_cache() -> None:
    """测试用：清空并重置计数。"""
    global _cache
    _cache = TTLCache()
