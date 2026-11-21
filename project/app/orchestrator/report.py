"""报告合成：把工具结果写成一段人话。

没有 key 时用模板（这里的 `compose`）；接上 LLM 后，模板退化成兜底与事实校验
的对照物——数字一律来自工具结果，模型只负责组织语言。
"""

from __future__ import annotations

from typing import Any

from app.orchestrator.intents import Intent

# 根因码 → 兜底建议：策略库没匹配到卡时用，保证报告不会开天窗。
NEXT_STEP: dict[str, str] = {
    "R1": "对比竞品价格带，先对 2~3 个高销量 SKU 做限时跟价，别全店跟",
    "R2": "换主图：补场景图与模特图，把文字占比压到 0.35 以下，A/B 两天看点击率",
    "R3": "优先补货断货 SKU，同时对同款做替代推荐，别让流量空转",
    "R4": "补尺码表与面料说明，详情页加「偏小一码」提示，降低退货",
    "R5": "收缩低 ROI 计划，把预算挪到搜索与私域的确定性流量",
    "R6": "先处理差评源头（质量/物流），再谈投放，否则流量越买越亏",
    "R7": "大促后先稳住价格与库存，用小额优惠券修复转化，别急着加投放",
    "R8": "查竞品动作与自然流量结构，补内容与新品种草",
    "R9": "把主力款价格带回主流价格带，或用组合装拉客单",
    "R10": "催物流商时效，主动触达延迟订单，避免退款与差评连锁",
}

RISK_LABELS: dict[int, str] = {0: "低风险", 1: "需人审", 2: "高风险·必须人审"}
DIRECTION_WORDS: dict[str, str] = {"down": "下降", "up": "上升"}


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _effect_text(effect: dict[str, Any] | None) -> str:
    """把 expected_effect 写成「预期 7 天内 stockout_rate 下降 15%~35%」。"""
    if not effect:
        return ""
    lo, hi = (list(effect.get("range") or []) + [None, None])[:2]
    if lo is None or hi is None:
        return ""
    metric = effect.get("metric") or "指标"
    word = DIRECTION_WORDS.get(str(effect.get("direction")), str(effect.get("direction") or ""))
    window = effect.get("window_days") or 7
    return f"预期 {window} 天内 {metric} {word} {lo:.0%}~{hi:.0%}"


def _action_lines(playbook: dict[str, Any] | None, limit_cards: int = 3) -> list[str]:
    """策略库给的动作：每张诊断卡最多两条，风险必须明写。

    同一条策略卡可能同时匹配多张诊断卡（比如两个断货 SKU 都落到 PB-R3-01），
    报告里只保留第一次出现，避免同一句话念三遍。
    """
    items = (playbook or {}).get("items") or []
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in items[:limit_cards]:
        for action in (item.get("actions") or [])[:2]:
            key = (str(item.get("code")), str(action.get("card_id")))
            if key in seen:
                continue
            seen.add(key)
            risk = int(action.get("risk", 0))
            label = action.get("action_label") or action.get("action_type") or ""
            tail = "，".join(
                p
                for p in (
                    label,
                    f"risk {risk}（{RISK_LABELS.get(risk, '未知')}）",
                    _effect_text(action.get("expected_effect")),
                )
                if p
            )
            lines.append(f"· {item['code']} {item['title']} → {action['title']}（{tail}）")
    return lines


RISK_TEXT = {0: "低风险·可自动执行", 1: "需人审", 2: "高风险·必须人审"}


def _draft_lines(drafts: dict[str, Any] | None, limit: int = 4) -> list[str]:
    """把已起草的动作写成「谁、做什么、什么风险、为什么」四段式短句。"""
    items = (drafts or {}).get("items") or []
    lines: list[str] = []
    for action in items[:limit]:
        risk = int(action.get("risk_level", 1))
        why = (action.get("reason") or action.get("card_title") or "").strip()
        tail = RISK_TEXT.get(risk, f"risk {risk}")
        if why:
            tail = f"{tail}；依据：{why[:40]}"
        lines.append(f"· {action.get('summary') or action.get('type')}（{tail}）")
    skipped = (drafts or {}).get("meta", {}).get("skipped") or []
    if skipped:
        first = skipped[0]
        reason = first.get("reason") if isinstance(first, dict) else str(first)
        lines.append(f"· 另有 {len(skipped)} 条没起草：{str(reason)[:40]}")
    return lines


def compose(
    intent: Intent,
    overview: dict[str, Any] | None,
    diagnose: dict[str, Any] | None,
    playbook: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
) -> list[str]:
    """返回若干段文字，编排层按段落流式发出。"""
    paras: list[str] = []

    if overview:
        kpis = {k["name"]: k for k in overview.get("kpis", [])}
        gmv = kpis.get("gmv", {})
        orders = kpis.get("paid_orders", {})
        health = overview.get("health", {})
        paras.append(
            f"截至 {overview.get('as_of')} 的 {intent.window_days} 天：成交额 "
            f"{gmv.get('value', 0):,.0f} 元（{_pct(gmv.get('delta_pct'))}），支付订单 "
            f"{orders.get('value', 0):,.0f}（{_pct(orders.get('delta_pct'))}），"
            f"健康分 {health.get('score', '—')}（{health.get('grade', '—')} 级）。"
        )

    if diagnose:
        paras.append(diagnose["gmv"]["summary"])
        paras.append(diagnose["funnel"]["summary"])

        cards = diagnose.get("cards", [])
        caveat_bag: set[str] = set()
        if cards:
            paras.append(f"定位到 {len(cards)} 个问题，按影响金额排序：")
            for i, card in enumerate(cards, 1):
                # 影响金额与占比已经写在 card["narrative"] 里，这里不再重复一遍
                paras.append(
                    f"{i}. {card['code']} {card['title']}（{card['scope']['dim_label']}"
                    f"「{card['scope']['dim_value']}」，置信度 {card['confidence'] * 100:.0f}%）："
                    f"{card['narrative']}"
                )
                caveat_bag.update(card.get("caveats") or [])

        promo = (diagnose.get("meta") or {}).get("promo_share") or {}
        skew = promo.get("skew")
        if skew is not None and abs(skew) > 0.2 and not any("促销日占比变化" in c for c in caveat_bag):
            caveat_bag.add(f"窗口内促销日占比变化 {skew:+.2f}，量价对比需要打折看")

        if caveat_bag:
            paras.append("口径与假设：" + "；".join(sorted(caveat_bag)))

        codes = sorted({c["code"] for c in cards})
        action_lines = _action_lines(playbook)
        if action_lines:
            risk2 = any(
                int(a.get("risk", 0)) >= 2
                for item in ((playbook or {}).get("items") or [])
                for a in (item.get("actions") or [])
            )
            paras.append("建议动作（按诊断卡）：" + ("，标 risk 2 的需人工审批" if risk2 else ""))
            paras.extend(action_lines)
            draft_lines = _draft_lines(actions)
            if draft_lines:
                paras.append("已起草为待审批动作（在「执行」页确认后生效）：")
                paras.extend(draft_lines)
        else:
            hints = [NEXT_STEP[c] for c in codes if c in NEXT_STEP]
            if hints:
                paras.append("下一步可以这样动：")
                paras.extend(f"· {h}" for h in hints[:3])

    if not diagnose:
        paras.append("想定位原因，可以接着问「为什么这周成交额下滑」，我会按款式/渠道/商品拆到根因。")

    if not paras:
        paras.append("这次没取到数据，先确认后端与数据文件是否就绪（/api/health）。")
    return paras
