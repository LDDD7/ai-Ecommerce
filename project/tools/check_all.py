"""一键离线自检：跑完全部不花外部额度的检查，输出一张汇总表。

用法（在 project/ 下）：
    python -m tools.check_all              # 默认：测试 + 策略卡 + 素材评测(mock) + 兼容性
    python -m tools.check_all --full       # 再加：数据自检 + 语义层冒烟 + 诊断冒烟（约 +5s）
    python -m tools.check_all --only tests # 只跑某一项

所有子进程都用当前解释器，且强制 LLM_MODE=mock / VISION_MODE=mock，
不会调用任何外部模型。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# name -> (argv, 说明, 是否属于 --full)
CHECKS: dict[str, tuple[list[str], str, bool]] = {
    "tests": (["-m", "pytest", "-q"], "pytest 全量（单测 + 接口）", False),
    "playbook": (["-m", "tools.playbook_lint"], "27 张策略卡校验", False),
    "vision": (["-m", "tools.vision_eval", "--mode", "mock", "--cases", "10"], "素材属性抽评测（回放）", False),
    "compat": (["-m", "tools.compat_check"], "环境适配自检", False),
    "data": (["tools/sanity_check.py"], "数据自检 + 10 类根因逐条对比", True),
    "metrics": (["-m", "tools.metrics_smoke"], "指标语义层冒烟", True),
    "diagnosis": (["-m", "tools.diagnosis_smoke", "2026-11-17", "3"], "诊断卡全文（含 Critic）", True),
}

TIMEOUT_S = 600


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["LLM_MODE"] = "mock"          # 断掉一切真模型调用
    env["VISION_MODE"] = "mock"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(PROJECT)
    return env


def run_one(name: str) -> tuple[bool, float, str]:
    argv, _desc, _full = CHECKS[name]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, *argv],
            cwd=PROJECT,
            env=_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
        )
        elapsed = time.perf_counter() - started
        output = (proc.stdout or "") + (proc.stderr or "")
        tail = "\n".join([line for line in output.strip().splitlines() if line.strip()][-3:])
        return proc.returncode == 0, elapsed, tail
    except subprocess.TimeoutExpired:
        return False, time.perf_counter() - started, f"超时（>{TIMEOUT_S}s）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线一键自检")
    parser.add_argument("--full", action="store_true", help="追加数据/语义层/诊断三项（约 +5s）")
    parser.add_argument("--only", choices=sorted(CHECKS), help="只跑一项")
    args = parser.parse_args(argv)

    if args.only:
        names = [args.only]
    else:
        names = [name for name, (_a, _d, full) in CHECKS.items() if args.full or not full]

    print(f"离线自检：{len(names)} 项（LLM_MODE=mock / VISION_MODE=mock，不调用外部模型）\n")
    results: list[tuple[str, bool, float, str]] = []
    for name in names:
        desc = CHECKS[name][1]
        print(f"  → {name:<10} {desc} ...", end="", flush=True)
        ok, elapsed, tail = run_one(name)
        results.append((name, ok, elapsed, tail))
        print(f" {'OK' if ok else 'FAIL'}  {elapsed:.1f}s")

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 56)
    for name, ok, elapsed, tail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<10} {elapsed:6.1f}s")
        if not ok and tail:
            for line in tail.splitlines():
                print(f"      {line}")
    print("=" * 56)
    total = sum(r[2] for r in results)
    if failed:
        print(f"结果：{len(results) - len(failed)}/{len(results)} 通过，用时 {total:.1f}s")
        return 1
    print(f"结果：全部通过（{len(results)} 项，用时 {total:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
