"""评测跑批入口（T7）。

用法::

    python -m app.eval.runner                       # 四个套件全跑
    python -m app.eval.runner --suite query --limit 5
    python -m app.eval.runner --json --out .tmp/eval.json

设计要点：

- 套件是**懒加载**的：某个套件 import 失败（例如缺 torch）只让那一个套件报
  ``error``，不拖垮整体跑批。
- 跑批只负责「跑 + 出数」，判定阈值在 ``app.eval.gate``；两者分开是为了让
  报告能先跑出来、阈值后定（spec §12.5 的口径：跑出来再写）。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from app.eval.gate import evaluate as evaluate_gate
from app.eval.types import SuiteResult

SUITE_ORDER = ("query", "diagnose", "action", "vision")
_MODULES = {
    "query": "app.eval.suites.query_suite",
    "diagnose": "app.eval.suites.diagnose_suite",
    "action": "app.eval.suites.action_suite",
    "vision": "app.eval.suites.vision_suite",
}


def run_suite(name: str, db_path: str | Path, *, limit: int | None = None) -> SuiteResult:
    """跑单个套件；任何异常都收敛成带 ``error`` 的 SuiteResult。"""
    module_path = _MODULES.get(name)
    if not module_path:
        return SuiteResult(suite=name, cases=[], error=f"未知套件：{name}")
    started = time.perf_counter()
    try:
        module = importlib.import_module(module_path)
        got = module.run(str(db_path), limit=limit)
        if not isinstance(got, SuiteResult):
            raise TypeError(f"{module_path}.run 必须返回 SuiteResult，实际是 {type(got).__name__}")
        return got
    except Exception as exc:  # noqa: BLE001 - 套件失败不能中断整体跑批
        return SuiteResult(
            suite=name,
            cases=[],
            elapsed_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_all(
    db_path: str | Path,
    *,
    suites: tuple[str, ...] = SUITE_ORDER,
    limit: int | None = None,
) -> dict[str, SuiteResult]:
    return {name: run_suite(name, db_path, limit=limit) for name in suites}


def summarize(results: dict[str, SuiteResult]) -> list[dict[str, Any]]:
    """把结果摊成表格行，精度指标优先取套件 meta 里的（更贴近验收口径）。"""
    rows: list[dict[str, Any]] = []
    for name, result in results.items():
        meta = result.meta or {}
        rows.append(
            {
                "suite": name,
                "total": result.total,
                "passed": result.passed,
                "accuracy": round(result.accuracy, 4),
                "mean_score": round(result.mean_score, 4),
                "elapsed_ms": round(result.elapsed_ms, 1),
                "meta": {k: meta[k] for k in sorted(meta) if k != "cases"},
                "error": result.error,
            }
        )
    return rows


def format_table(rows: list[dict[str, Any]]) -> str:
    header = f"{'套件':<10}{'用例':>5}{'通过':>5}{'准确率':>9}{'均分':>8}{'耗时(ms)':>11}"
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row['suite']:<10}{row['total']:>5}{row['passed']:>5}"
            f"{row['accuracy']:>9.3f}{row['mean_score']:>8.3f}{row['elapsed_ms']:>11.1f}"
        )
    return "\n".join(lines)


def format_details(rows: list[dict[str, Any]]) -> str:
    """把 meta 里的验收口径逐条摊开（诊断的 precision/recall 在这里）。"""
    lines: list[str] = []
    for row in rows:
        if not row["meta"]:
            continue
        pairs = ", ".join(f"{k}={v}" for k, v in row["meta"].items())
        lines.append(f"- {row['suite']}: {pairs}")
    return "\n".join(lines)


def build_report(results: dict[str, SuiteResult], *, db_path: str | Path, gates: dict[str, float] | None = None) -> dict[str, Any]:
    report = {
        "db": str(db_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "suites": {name: result.as_dict() for name, result in results.items()},
        "rows": summarize(results),
    }
    report["gate"] = evaluate_gate(results, gates).as_dict()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="店诊 Agent 评测跑批")
    parser.add_argument("--db", default="data/store.db", help="SQLite 库路径")
    parser.add_argument("--suite", action="append", choices=SUITE_ORDER, help="只跑指定套件，可重复")
    parser.add_argument("--limit", type=int, default=None, help="每个套件只跑前 N 条")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认输出表格）")
    parser.add_argument("--out", default=None, help="把 JSON 报告写到文件")
    parser.add_argument("--no-gate", action="store_true", help="不出门禁结论")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"库不存在：{db_path}", file=sys.stderr)
        return 2
    suites = tuple(args.suite) if args.suite else SUITE_ORDER
    started = time.perf_counter()
    results = run_all(db_path, suites=suites, limit=args.limit)
    total_ms = (time.perf_counter() - started) * 1000

    rows = summarize(results)
    if args.json:
        report = build_report(results, db_path=db_path)
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        print(text)
    else:
        print(format_table(rows))
        details = format_details(rows)
        if details:
            print()
            print("验收口径")
            print(details)
        print()
        print(f"总耗时 {total_ms:.0f}ms")
        if not args.no_gate:
            gate = evaluate_gate(results)
            print(gate.format())
    if args.out and not args.json:
        report = build_report(results, db_path=db_path)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if all(not r.error for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
