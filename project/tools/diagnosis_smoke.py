"""诊断卡冒烟：把 `build_diagnosis` 的结果打成人能读的样子。

用法（在 project/ 下）：
    python -m tools.diagnosis_smoke                # 用 .env 里的 demo_as_of
    python -m tools.diagnosis_smoke 2026-10-20     # 指定 as_of
    python -m tools.diagnosis_smoke 2026-11-06 3   # 指定 as_of + 卡数
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.diagnose.diagnosis import build_diagnosis  # noqa: E402
from app.diagnose.evidence import fmt_num  # noqa: E402

DB = ROOT / "data" / "store.db"


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    as_of = argv[1] if len(argv) > 1 else None
    max_cards = int(argv[2]) if len(argv) > 2 else 5

    report = build_diagnosis(DB, as_of=as_of, max_cards=max_cards)
    meta = report["meta"]

    print(f"诊断日 {report['as_of']}  窗口 {report['window']['start']}~{report['window']['end']}"
          f"  基线 {report['baseline_window']['start']}~{report['baseline_window']['end']}")
    print(f"异常点 {meta['points']} 个 / 分组 {meta['groups']} 个 / 出卡 {len(report['cards'])} 张"
          f" / 丢弃 {len(meta['dropped'])} 组 / 耗时 {meta['elapsed_ms']}ms")
    promo = meta.get("promo_share") or {}
    if promo:
        print(f"促销偏斜 {promo.get('skew'):+.2f}（当期 {promo.get('window'):.2f} / 基线 {promo.get('baseline'):.2f}）")
    critic = meta.get("critic") or {}
    if critic:
        print(f"Critic {critic.get('engine')} 审查 {critic.get('checked')} 张："
              f"成立 {critic.get('support')} / 存疑 {critic.get('weaken')} / 驳回 {critic.get('reject')}"
              f"（{critic.get('elapsed_ms')}ms）")
    print()
    print("成交额分解：" + report["gmv"]["summary"])
    print("漏斗：" + report["funnel"]["summary"])
    print()

    for i, card in enumerate(report["cards"], 1):
        impact = card["gmv_impact"]
        share = card["impact_share"]
        money = "—" if impact is None else f"{impact:+,.0f} 元"
        if share is None:
            share_s = ""
        else:
            word = "降幅" if (impact or 0) < 0 else "增幅"
            share_s = f"（占整体{word} {abs(share) * 100:.0f}%）"
        print(f"[{i}] {card['code']} {card['title']}  [{card['severity']}] 置信度 {card['confidence']:.0%}")
        print(f"    影响 {money}{share_s}｜主症状 {card['primary_metric']}")
        print(f"    {card['narrative']}")
        for ev in card["evidence"]:
            pct = "—" if ev["delta_pct"] is None else f"{ev['delta_pct'] * 100:+.1f}%"
            z = "" if ev["z"] is None else f" z={ev['z']:.1f}"
            print(f"      · {ev['display']}({ev['dim_field']}={ev['dim_value']}) "
                  f"{fmt_num(ev['baseline'], ev['unit'])}→{fmt_num(ev['current'], ev['unit'])} {pct}{z}")
        others = [c for c in card["candidates"][1:]]
        if others:
            print("      备选：" + "、".join(f"{c['code']} {c['title']} {c['confidence']:.0%}" for c in others))
        for cav in card["caveats"]:
            print(f"      ! {cav}")
        print()

    if critic.get("verdicts"):
        print("Critic 逐卡结论：" + "；".join(
            f"{v['card_id']} {v['code']} {v['verdict']}"
            f" {v['confidence_before']:.2f}→{v['confidence_after']:.2f}"
            + (f"（{v['objections'][0]}）" if v.get("objections") else "")
            for v in critic["verdicts"]
        ))
    if meta.get("dropped"):
        print("被丢弃的分组：" + "、".join(f"{d['dim']}={d['value']}({d['reason']})" for d in meta["dropped"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
