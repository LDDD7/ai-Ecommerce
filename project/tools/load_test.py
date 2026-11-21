"""压测脚本（T10）：对着一台**已经在跑**的后端打并发，统计延迟分位与错误率。

为什么不用 locust / wrk：
- 本机没有 wrk，locust 要额外装包，而这个项目只是要看「20~30 并发下会不会崩、P95 是多少」；
- 需要按接口混合打（健康检查 / 概览 / 问数 / 诊断），还要能读 JSON 结果做回归对比；
- 300 行 httpx asyncio 足够，且能直接进 `tools/`。

用法（先起后端：`python -m tools.dev --skip-checks`，另一个窗口）：

    python -m tools.load_test --base-url http://127.0.0.1:8000 --suite mixed --concurrency 20 --requests 200
    python -m tools.load_test --suite heavy --concurrency 30 --requests 60 --json --out .tmp/load.json

`--suite` 三种：
- `light`：health + query（语义层），几乎不吃 CPU，用来看框架本身的开销；
- `mixed`：health / overview / query / playbook.search 混合，接近「一个人在页面上点」；
- `heavy`：全是 diagnose（1.5s 级，含 SQLite 全表扫），用来看最坏情况。

判定（spec §12.4）：错误率 < 1%、非 heavy 套件 P95 ≤ 3s、heavy 套件 P95 ≤ 8s。
退出码 1 表示有指标越线，方便进 CI。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

#: 每个套件 = [(方法, 路径, 请求体/None, 权重)]
SUITES: dict[str, list[tuple[str, str, dict | None, int]]] = {
    "light": [
        ("GET", "/api/health", None, 5),
        ("POST", "/api/query", {"spec": {
            "metrics": ["gmv", "paid_orders", "aov"],
            "time": {"start": "2026-11-11", "end": "2026-11-17"},
        }}, 5),
    ],
    "mixed": [
        ("GET", "/api/health", None, 3),
        ("GET", "/api/overview?days=7", None, 3),
        ("POST", "/api/query", {"spec": {
            "metrics": ["gmv", "paid_orders", "aov"],
            "time": {"start": "2026-11-11", "end": "2026-11-17"},
        }}, 2),
        ("POST", "/api/playbook/search", {"root_causes": ["R4"], "limit": 3}, 2),
    ],
    "heavy": [
        ("GET", "/api/diagnose?as_of=2026-11-17", None, 1),
    ],
}

#: 每个套件的 P95 上限（毫秒）
P95_LIMIT_MS = {"light": 3000, "mixed": 3000, "heavy": 8000}
MAX_ERROR_RATE = 0.01


@dataclass
class Sample:
    method: str
    path: str
    status: int
    elapsed_ms: float
    ok: bool
    error: str = ""


@dataclass
class Report:
    suite: str
    concurrency: int
    elapsed_s: float
    samples: list[Sample] = field(default_factory=list)

    def _ok_samples(self) -> list[Sample]:
        return [s for s in self.samples if s.ok]

    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def errors(self) -> int:
        return sum(1 for s in self.samples if not s.ok)

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total else 0.0

    @property
    def rps(self) -> float:
        return self.total / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def percentiles(self) -> dict[str, float]:
        values = sorted(s.elapsed_ms for s in self.samples)
        if not values:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0}

        def pick(q: float) -> float:
            idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
            return round(values[idx], 1)

        return {
            "p50": pick(0.50),
            "p95": pick(0.95),
            "p99": pick(0.99),
            "mean": round(statistics.fmean(values), 1),
        }

    def by_endpoint(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for sample in self.samples:
            key = f"{sample.method} {sample.path.split('?')[0]}"
            row = out.setdefault(key, {"n": 0, "errors": 0, "sum_ms": 0.0, "max_ms": 0.0})
            row["n"] += 1
            row["errors"] += 0 if sample.ok else 1
            row["sum_ms"] += sample.elapsed_ms
            row["max_ms"] = max(row["max_ms"], sample.elapsed_ms)
        for row in out.values():
            row["avg_ms"] = round(row["sum_ms"] / row["n"], 1)
            row["max_ms"] = round(row["max_ms"], 1)
            del row["sum_ms"]
        return out

    def verdict(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        p95 = self.percentiles()["p95"]
        limit = P95_LIMIT_MS.get(self.suite, 3000)
        if self.error_rate >= MAX_ERROR_RATE:
            reasons.append(f"错误率 {self.error_rate:.2%} ≥ {MAX_ERROR_RATE:.0%}")
        if p95 > limit:
            reasons.append(f"P95 {p95:.0f}ms > {limit}ms")
        return (not reasons, reasons)

    def as_dict(self) -> dict:
        ok, reasons = self.verdict()
        return {
            "suite": self.suite,
            "concurrency": self.concurrency,
            "total": self.total,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "elapsed_s": round(self.elapsed_s, 2),
            "rps": round(self.rps, 2),
            "latency_ms": self.percentiles(),
            "by_endpoint": self.by_endpoint(),
            "limits": {"p95_ms": P95_LIMIT_MS.get(self.suite, 3000), "max_error_rate": MAX_ERROR_RATE},
            "passed": ok,
            "failures": reasons,
        }

    def format(self) -> str:
        p = self.percentiles()
        ok, reasons = self.verdict()
        lines = [
            f"套件 {self.suite}　并发 {self.concurrency}　请求 {self.total}　耗时 {self.elapsed_s:.1f}s　RPS {self.rps:.1f}",
            f"延迟(ms)  P50 {p['p50']:.0f}　P95 {p['p95']:.0f}　P99 {p['p99']:.0f}　均值 {p['mean']:.0f}",
            f"错误 {self.errors} 条（{self.error_rate:.2%}）",
            "",
            f"{'接口':<34}{'次数':>6}{'失败':>6}{'均值ms':>9}{'最大ms':>9}",
            "-" * 66,
        ]
        for key, row in sorted(self.by_endpoint().items()):
            lines.append(
                f"{key:<34}{int(row['n']):>6}{int(row['errors']):>6}{row['avg_ms']:>9.1f}{row['max_ms']:>9.1f}"
            )
        lines.append("")
        lines.append("结论：" + ("通过" if ok else "未通过（" + "；".join(reasons) + "）"))
        return "\n".join(lines)


def _pick(rng, weights: list[int]) -> int:
    total = sum(weights)
    x = rng.random() * total
    upto = 0.0
    for idx, w in enumerate(weights):
        upto += w
        if x <= upto:
            return idx
    return len(weights) - 1


async def run(
    *,
    base_url: str,
    suite: str,
    concurrency: int,
    requests: int,
    timeout_s: float,
    warmup: int,
) -> Report:
    plan = SUITES[suite]
    methods = [p[0] for p in plan]
    paths = [p[1] for p in plan]
    bodies = [p[2] for p in plan]
    weights = [p[3] for p in plan]

    import random

    rng = random.Random(20261121)
    queue = asyncio.Queue()
    for _ in range(requests):
        i = _pick(rng, weights)
        queue.put_nowait(i)

    samples: list[Sample] = []

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
        # 预热：把 SQLite 页缓存、线程池、模型句柄都点一次
        for i in range(min(warmup, len(plan))):
            try:
                await client.request(methods[i], paths[i], json=bodies[i])
            except Exception:  # noqa: BLE001 - 预热失败不计入统计
                pass

        async def worker() -> None:
            while True:
                try:
                    idx = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                started = time.perf_counter()
                try:
                    resp = await client.request(methods[idx], paths[idx], json=bodies[idx])
                    elapsed = (time.perf_counter() - started) * 1000
                    ok = resp.status_code < 400
                    samples.append(
                        Sample(
                            method=methods[idx],
                            path=paths[idx],
                            status=resp.status_code,
                            elapsed_ms=elapsed,
                            ok=ok,
                            error="" if ok else resp.text[:160],
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    elapsed = (time.perf_counter() - started) * 1000
                    samples.append(
                        Sample(methods[idx], paths[idx], 0, elapsed, False, f"{type(exc).__name__}: {exc}")
                    )

        started_all = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed_all = time.perf_counter() - started_all

    return Report(suite=suite, concurrency=concurrency, elapsed_s=elapsed_all, samples=samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后端压测（20~30 并发）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--suite", choices=sorted(SUITES), default="mixed")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    try:
        report = asyncio.run(
            run(
                base_url=args.base_url,
                suite=args.suite,
                concurrency=args.concurrency,
                requests=args.requests,
                timeout_s=args.timeout,
                warmup=args.warmup,
            )
        )
    except httpx.ConnectError:
        print(f"连不上 {args.base_url}：先 `python -m tools.dev --skip-checks` 起后端")
        return 1

    print(report.format())
    if args.json or args.out:
        payload = json.dumps(report.as_dict(), ensure_ascii=False, indent=1)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(payload, encoding="utf-8")
            print(f"\n已写入 {out}")
        if args.json:
            print(payload)
    return 0 if report.verdict()[0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
