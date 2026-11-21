"""策略库（Playbook）校验工具。

用法：
    python -m tools.playbook_lint [--cards-dir DIR] [--min-cards 27] [--allow-missing]

检查项：
    1. 卡片能通过 loader 校验（字段/枚举/range 由 pydantic 保证），card_id 唯一；
    2. R1..R10 每类根因至少被 1 张卡覆盖；
    3. expected_effect.metric 与 success_metric 都在指标目录 catalog 的 23 个指标内
       （catalog 不可用时跳过并提示）；
    4. 卡片数量不少于 --min-cards。

--allow-missing 的含义：策略库可能还在并行补写，此时「卡片数不足」与「根因覆盖
不全」由错误降级为警告，方便 CI 在写卡阶段先跑结构校验；非法卡、重复 id、
指标越界、文件名不一致仍然是错误。

退出码：存在错误 → 1；否则 0。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playbook.loader import CARDS_DIR, PlaybookError, by_root, load_cards  # noqa: E402
from app.playbook.schema import ACTION_TYPES, ROOT_CAUSES  # noqa: E402

RISK_LABELS = {
    0: "无需审批（内容/自检）",
    1: "需人工审批（投放/素材）",
    2: "高风险（价格/资金）",
}

ACTION_LABELS = {
    "adjust_price": "调价",
    "adjust_bid": "调出价",
    "swap_creative": "换素材",
    "issue_coupon": "发券",
    "restock_alert": "补货预警",
}

# 中日韩全角字符在终端里占两列，表格对齐要按显示宽度算而不是 len()。
_WIDE = {"W", "F"}


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in _WIDE else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """按显示宽度对齐打印一张三列表格（带表头）。"""
    widths = [_width(cell) for cell in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))
    for line in (header, *rows):
        print("  " + "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(line)).rstrip())


def _load_metric_names() -> tuple[list[str] | None, str | None]:
    """取 catalog 指标名；取不到时返回 (None, 原因)，不阻断校验。"""
    try:
        from app.metrics.spec import load_catalog

        return sorted(load_catalog().metrics), None
    except Exception as exc:  # noqa: BLE001 - 指标目录坏了也不该让 lint 崩
        return None, f"{type(exc).__name__}: {exc}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.playbook_lint",
        description="校验 app/playbook/cards 下的策略卡，打印根因/风险/动作类型分布。",
    )
    parser.add_argument("--cards-dir", default=None, help="卡片目录（默认 app/playbook/cards）")
    parser.add_argument("--min-cards", type=int, default=27, help="卡片数量下限（默认 27）")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="策略库尚未补齐时，数量不足与根因覆盖不全只报警告，不返回非零退出码",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 非交互式 stdout 可能不支持
        pass

    args = _build_parser().parse_args(argv)
    cards_dir = Path(args.cards_dir) if args.cards_dir else CARDS_DIR
    errors: list[str] = []
    warnings: list[str] = []

    print("策略库校验")
    print(f"目录：{cards_dir}")

    if not cards_dir.exists():
        print("\n[错误] 卡片目录不存在")
        return 1

    try:
        cards = load_cards(cards_dir)
    except PlaybookError as exc:  # 结构性错误：一张坏卡都不放过，直接列出全部问题
        print("\n[错误] 策略卡加载失败：")
        for line in str(exc).splitlines():
            print(f"  {line}")
        print("\n校验失败：策略卡存在结构性问题，未生成分布统计")
        return 1

    # 1) card_id 唯一（loader 已经拦过一遍，这里兜底并让报告自洽）
    dup_ids = sorted(cid for cid, n in Counter(c.card_id for c in cards).items() if n > 1)
    for cid in dup_ids:
        errors.append(f"card_id 重复：{cid}")

    print(f"卡片总数：{len(cards)}（下限 {args.min_cards}）")

    # 2) 根因覆盖 R1..R10
    index = by_root(cards)
    print("\n按根因覆盖（R1..R10）：")
    rows: list[tuple[str, ...]] = []
    for code, name in ROOT_CAUSES.items():
        count = len(index.get(code, []))
        rows.append((code, name, str(count)))
    _print_table(("代码", "根因", "卡片数"), rows)
    covered = sum(1 for code in ROOT_CAUSES if index.get(code))
    print(f"  覆盖 {covered}/{len(ROOT_CAUSES)} 类根因")

    missing = [code for code in ROOT_CAUSES if not index.get(code)]
    for code in missing:
        message = f"根因 {code}（{ROOT_CAUSES[code]}）没有任何卡片覆盖"
        if args.allow_missing:
            warnings.append(message)
        else:
            errors.append(message)

    # 3) 风险等级分布
    risk_counter = Counter(card.risk for card in cards)
    print("\n按风险等级：")
    _print_table(
        ("等级", "含义", "卡片数"),
        [(str(risk), RISK_LABELS[risk], str(risk_counter.get(risk, 0))) for risk in (0, 1, 2)],
    )

    # 4) 动作类型分布
    action_counter = Counter(card.action_type for card in cards)
    print("\n按动作类型：")
    _print_table(
        ("动作类型", "说明", "卡片数"),
        [(action, ACTION_LABELS[action], str(action_counter.get(action, 0))) for action in ACTION_TYPES],
    )

    # 5) 指标是否在 catalog 内
    metric_names, catalog_error = _load_metric_names()
    if metric_names is None:
        print(f"\n指标核查：跳过（catalog 不可用：{catalog_error}）")
    else:
        print(f"\n指标核查：catalog 共 {len(metric_names)} 个指标")
        known = set(metric_names)
        bad_lines: list[str] = []
        for card in cards:
            effect_metric = card.expected_effect.metric
            if effect_metric not in known:
                bad_lines.append(
                    f"{card.card_id}: expected_effect.metric={effect_metric!r} 不在 catalog"
                )
            if card.success_metric not in known:
                bad_lines.append(
                    f"{card.card_id}: success_metric={card.success_metric!r} 不在 catalog"
                )
            extra = [m for m in card.metrics if m not in known]
            if extra:
                warnings.append(f"{card.card_id}: metrics 含 catalog 外指标 {extra}")
        errors.extend(bad_lines)
        if bad_lines:
            for line in bad_lines:
                print(f"  [错误] {line}")
        else:
            print("  全部通过")

    # 6) 数量下限
    if len(cards) < args.min_cards:
        message = f"卡片数量 {len(cards)} < 下限 {args.min_cards}"
        if args.allow_missing:
            warnings.append(message + "（--allow-missing，暂不计为错误）")
        else:
            errors.append(message)

    print("\n" + "-" * 60)
    for message in errors:
        print(f"[错误] {message}")
    for message in warnings:
        print(f"[警告] {message}")

    if errors:
        print(f"\n校验失败：{len(errors)} 个错误，{len(warnings)} 个警告")
        return 1
    print(f"\n校验通过：0 个错误，{len(warnings)} 个警告")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
