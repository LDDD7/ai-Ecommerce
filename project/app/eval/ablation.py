"""消融实验（T7）：证明每个组件不是摆设。

两类：

1. **检索四档**（spec §12.2）：``off``（只用 FTS 关键词）→ ``rule``（加规则分）
   → ``cross``（加 cross-encoder 过滤）→ ``llm``（加 listwise 重排）。
   指标是「策略卡 top-3 是否命中该根因的卡」。
2. **管线开关**：同一批诊断场景，``with_replan`` 开/关，看精度与召回的变化。

跨档默认不跑 ``llm``（要真花钱），只有显式 ``--llm`` 才跑。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.eval.metrics import precision_recall, topk_hit

# 检索消融的查询集：每条给「期望出现的根因码」（卡上的 root_causes 含该码即算命中）。
RETRIEVAL_QUERIES: list[dict[str, Any]] = [
    {"query": "退款率上升 尺码偏小 详情页没有尺码表", "codes": ["R4"]},
    {"query": "投放 ROI 恶化 消耗上升 转化不涨", "codes": ["R5"]},
    {"query": "大促结束后流量断崖 促销透支", "codes": ["R7"]},
    {"query": "竞品降价 转化率下滑", "codes": ["R1"]},
    {"query": "差评爆发 一星评价增多", "codes": ["R6"]},
    {"query": "热销款断货 库存不足", "codes": ["R3"]},
    {"query": "曝光下滑 竞争加剧 自然流量变少", "codes": ["R8"]},
]


@dataclass(slots=True)
class AblationRow:
    group: str
    variant: str
    cases: int
    hit_rate: float
    precision: float
    recall: float
    elapsed_ms: float
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "variant": self.variant,
            "cases": self.cases,
            "hit_rate": round(self.hit_rate, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "elapsed_ms": round(self.elapsed_ms, 1),
            **({"extra": self.extra} if self.extra else {}),
        }


def recall_ablation(
    db_path: str | Path,
    *,
    queries: Sequence[dict[str, Any]] | None = None,
    limit: int = 5,
) -> list[AblationRow]:
    """召回层消融：只用 FTS（trigram 关键词）vs FTS + 向量（bge-m3）。

    两档都开规则重排，只改召回源，这样差异只来自向量。
    """
    from app.playbook.search import search_playbook

    items = list(queries or RETRIEVAL_QUERIES)
    rows: list[AblationRow] = []
    for label, with_vec in (("fts-only", False), ("hybrid", True)):
        started = time.perf_counter()
        hits = 0
        top1_hits = 0
        for item in items:
            hit_list, _meta = search_playbook(
                str(db_path), item["query"], limit=limit, mode="rule", with_vec=with_vec
            )
            gold = set(item["codes"])
            card_sets = [set(hit.card.root_causes or []) for hit in hit_list]
            if any(codes & gold for codes in card_sets[:3]):
                hits += 1
            if card_sets and card_sets[0] & gold:
                top1_hits += 1
        rows.append(
            AblationRow(
                group="recall",
                variant=label,
                cases=len(items),
                hit_rate=hits / len(items) if items else 0.0,
                precision=top1_hits / len(items) if items else 0.0,
                recall=hits / len(items) if items else 0.0,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                extra={"top1_rate": round(top1_hits / len(items), 4), "with_vec": with_vec},
            )
        )
    return rows


def retrieval_ablation(
    db_path: str | Path,
    *,
    modes: Sequence[str] = ("off", "rule", "cross"),
    queries: Sequence[dict[str, Any]] | None = None,
    limit: int = 5,
) -> list[AblationRow]:
    """四个档位各跑一遍，返回每档一行。"""
    from app.playbook.search import search_playbook

    items = list(queries or RETRIEVAL_QUERIES)
    rows: list[AblationRow] = []
    for mode in modes:
        started = time.perf_counter()
        hits = 0
        top1_hits = 0
        degraded = 0
        for item in items:
            hit_list, meta = search_playbook(str(db_path), item["query"], limit=limit, mode=mode)
            gold = item["codes"]
            # 每张卡带 1~3 个 root_causes；命中判定看「该卡的根因集里有没有真值」。
            card_code_sets = [set(hit.card.root_causes or []) for hit in hit_list]
            if any(codes & set(gold) for codes in card_code_sets[:3]):
                hits += 1
            if card_code_sets and card_code_sets[0] & set(gold):
                top1_hits += 1
            if meta.get("degraded"):
                degraded += 1
        rows.append(
            AblationRow(
                group="retrieval",
                variant=mode,
                cases=len(items),
                hit_rate=hits / len(items) if items else 0.0,
                precision=top1_hits / len(items) if items else 0.0,
                recall=hits / len(items) if items else 0.0,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                extra={
                    "top1_rate": round(top1_hits / len(items), 4) if items else 0.0,
                    "degraded": degraded,
                    "limit": limit,
                },
            )
        )
    return rows


def pipeline_ablation(
    db_path: str | Path,
    *,
    limit: int = 8,
    with_replan_variants: Sequence[bool] = (False, True),
) -> list[AblationRow]:
    """同一批诊断场景跑 ``with_replan`` 两档（不跑全量 25 条，默认前 8 条）。"""
    from app.diagnose.diagnosis import build_diagnosis
    from app.eval.suites import diagnose_suite

    cases = diagnose_suite.cases(str(db_path))[:limit]
    rows: list[AblationRow] = []
    for flag in with_replan_variants:
        started = time.perf_counter()
        top1 = 0
        top3 = 0
        precisions: list[float] = []
        recalls: list[float] = []
        cards_total = 0
        for case in cases:
            payload = case.payload
            result = build_diagnosis(
                str(db_path),
                as_of=payload["as_of"],
                with_replan=flag,
            )
            pred = [card["code"] for card in result.get("cards", [])]
            gold = payload["expect"]
            cards_total += len(pred)
            if pred and pred[0] in gold:
                top1 += 1
            if topk_hit(pred, gold, 3):
                top3 += 1
            p, r = precision_recall(pred, gold)
            precisions.append(p)
            recalls.append(r)
        rows.append(
            AblationRow(
                group="pipeline",
                variant="replan=on" if flag else "replan=off",
                cases=len(cases),
                hit_rate=top3 / len(cases) if cases else 0.0,
                precision=statistics.fmean(precisions) if precisions else 0.0,
                recall=statistics.fmean(recalls) if recalls else 0.0,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                extra={
                    "top1_rate": round(top1 / len(cases), 4) if cases else 0.0,
                    "top3_rate": round(top3 / len(cases), 4) if cases else 0.0,
                    "avg_cards": round(cards_total / len(cases), 2) if cases else 0.0,
                },
            )
        )
    return rows


def format_rows(rows: Sequence[AblationRow]) -> str:
    header = f"{'组':<10}{'档位':<14}{'用例':>5}{'命中率':>8}{'精确率':>8}{'召回':>8}{'耗时(ms)':>11}"
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.group:<10}{row.variant:<14}{row.cases:>5}{row.hit_rate:>8.3f}"
            f"{row.precision:>8.3f}{row.recall:>8.3f}{row.elapsed_ms:>11.1f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="消融实验")
    parser.add_argument("--db", default="data/store.db")
    parser.add_argument("--kind", choices=("retrieval", "pipeline", "all"), default="all")
    parser.add_argument("--llm", action="store_true", help="检索消融时把 llm 档也跑上（会真调模型）")
    parser.add_argument("--limit", type=int, default=8, help="pipeline 组的用例数")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows: list[AblationRow] = []
    if args.kind in ("retrieval", "all"):
        modes = ("off", "rule", "cross", "llm") if args.llm else ("off", "rule", "cross")
        rows.extend(retrieval_ablation(args.db, modes=modes))
    if args.kind in ("pipeline", "all"):
        rows.extend(pipeline_ablation(args.db, limit=args.limit))

    if args.json:
        print(json.dumps([row.as_dict() for row in rows], ensure_ascii=False, indent=2))
    else:
        print(format_rows(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
