"""LLM 反方审查：在规则版 Critic 之上，让模型对「已经排好序、已经降过权」的卡再挑一遍毛病。

为什么需要它：规则版 ``critic.py`` 是纯函数，同样的卡永远得到同样的折扣，它看得懂
方向和口径，看不懂语义——维度值选错、主指标和结论不是一回事、一张卡把两件事说成
一件。这些只能靠语言理解，所以让模型补一刀。

边界（也是本模块的全部设计）：

1. **只降不升**。模型给的置信度和规则版取 ``min``，再由阈值重算 verdict。方向反过来
   就等于让 LLM 覆盖规则引擎的结论：规则版能复现、能对账、能离线回归，模型不能。
   权限只给到「打折」，这样即使模型抽风，最坏结果也只是结论变保守，不会凭空多出一个
   规则版没算出来的高分结论。
2. **只复查前 3 张**。诊断卡按影响金额降序，第 4 张起影响已经很小；4 秒预算花在尾部
   卡片上不划算，而单卡一次调用（不合并成一次批量）是为了让反对意见能精确归到某张卡，
   某一张失败也不牵连其它卡。
3. **任何失败都静默回退**。网关报错、JSON 结构不对、超预算，都让该卡保持规则版结果，
   只把原因记进 ``meta``。报告必须永远出得来，LLM 只是「可能让结论更保守」的增强。

不 import 具体的模型实现，只按 duck typing 调 ``llm.chat_json(...)``，注入即用；
``LLMError`` 只用来识别「网关级失败」，遇到就停止后续复查（后面几张大概率一样）。
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

from app.core.llm import LLMError
from app.diagnose.critic import REJECT_BELOW, SUPPORT_MIN, Verdict

# 只复查排在前面的几张卡：再往后的卡影响金额已经很小，不值得花预算。
LLM_CRITIC_CARDS = 3
# 整段复查的总预算（毫秒）。和报告链路 8s 的端到端预算对齐，留一半给叙述。
LLM_CRITIC_BUDGET_MS = 4000
# 模型当反方审查员时几乎给每张卡都是 0.5 上下；完全照它的分数走，所有卡都会被压成
# 「存疑」、排序失去意义。给一个地板：单张卡最多压 20%，模型价值主要在反方意见。
LLM_CONFIDENCE_FLOOR = 0.8

# 单卡提示词只喂这些：完整诊断 JSON 一张卡就有 2000+ 字，模型会转去抄表格。
MAX_EVIDENCE = 3
NARRATIVE_MAX_CHARS = 200
# 提示词要求每条 40 字，但 60 字以内仍然是有用信息，不值得为超长整卡回退，截断即可。
MAX_LLM_OBJECTIONS = 3
MAX_OBJECTION_CHARS = 60
# 规则版的 objections 可能已经有 3~6 条，合并后只留前 4 条，一屏放得下。
MAX_MERGED_OBJECTIONS = 4

LLM_MAX_TOKENS = 400
LLM_TEMPERATURE = 0.0

# 模型偶尔用这些词表示「没有意见」，当成反对意见展示会很难看，直接丢掉。
_EMPTY_OBJECTIONS = {"无", "没有", "暂无", "无明显问题", "none", "n/a", "na", "-", "—"}

SYSTEM_PROMPT = (
    "你是电商诊断报告的「反方审查员」，只挑毛病，不讲好话。\n"
    "硬规则：\n"
    "1. 只针对【待审卡片】里的事实提反对意见，只能引用其中出现过的数字，"
    "不得计算新数字，也不得编造事实。\n"
    "2. objections 每条不超过 40 字、最多 3 条；没有真问题就输出空数组，不要凑数。\n"
    "3. confidence 是你认为这张卡站得住的概率（0.0~1.0）；它只会被用来往下压，"
    "给得比卡片现有置信度高不会生效。\n"
    "4. verdict 只能取 support / weaken / reject 之一。\n"
    "5. 只输出 JSON，形如 "
    '{"objections": ["..."], "confidence": 0.4, "verdict": "weaken"}，不要解释。'
)


def _slim_scope(scope: Any) -> dict[str, Any]:
    """维度范围只留三个字段：够模型判断「这张卡说的是谁」。"""
    if not isinstance(scope, dict):
        return {}
    return {
        key: scope[key]
        for key in ("dim", "dim_label", "dim_value")
        if scope.get(key) is not None
    }


def _slim_evidence(evidence: Any) -> list[dict[str, Any]]:
    """证据最多 3 条，每条只保留对比用得上的数字字段（和 narrator 同口径）。"""
    if not isinstance(evidence, list):
        return []
    out: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        slim = {
            key: item[key]
            for key in ("metric", "dim_value", "baseline", "current", "delta_pct")
            if item.get(key) is not None
        }
        if slim:
            out.append(slim)
        if len(out) >= MAX_EVIDENCE:
            break
    return out


def _compact_facts(card: dict[str, Any], verdict: Verdict) -> dict[str, Any]:
    """把一张卡压成提示词事实：模型只需要能用来反驳的那几个字段。

    ``confidence`` 给的是规则版复核后的值（也就是用户真正会看到的数字），
    模型是对「这张卡现在有多少把握」提意见，而不是对原始置信度。
    """
    facts: dict[str, Any] = {
        "code": str(card.get("code") or verdict.code),
        "title": str(card.get("title") or verdict.title),
    }
    scope = _slim_scope(card.get("scope"))
    if scope:
        facts["scope"] = scope
    facts["primary_metric"] = card.get("primary_metric")
    facts["confidence"] = round(float(verdict.confidence_after), 3)
    # 空字段直接不写：``"evidence": []`` 这类空壳只会白占 token。
    evidence = _slim_evidence(card.get("evidence"))
    if evidence:
        facts["evidence"] = evidence
    narrative = str(card.get("narrative") or "")[:NARRATIVE_MAX_CHARS]
    if narrative:
        facts["narrative"] = narrative
    rule_objections = list(verdict.objections)[:MAX_MERGED_OBJECTIONS]
    if rule_objections:
        facts["rule_objections"] = rule_objections
    return facts


def _build_messages(facts: dict[str, Any]) -> list[dict[str, str]]:
    """system 定规则，user 只放压缩后的事实——风格照抄 ``narrator._build_messages``。"""
    body = json.dumps(facts, ensure_ascii=False)
    user = (
        "【待审卡片】\n"
        f"{body}\n\n"
        "请以反方审查员身份审这张卡：只指出它站不住的地方（每条 40 字内、最多 3 条），"
        "引用上面的数字，不要重复规则版已经说过的反对意见；"
        "再给出你认为站得住的置信度（0~1，只能更低）与 verdict。"
        "没有真问题就给空数组和不变或更低的置信度。只输出 JSON。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _to_confidence(raw: Any) -> float:
    """置信度必须能转成数字；转不了就让整张卡回退，绝不猜模型的意思。"""
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"confidence 不是数字：{raw!r}")
    try:
        value = float(raw.strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"confidence 不是数字：{raw!r}") from exc
    if value != value:  # NaN
        raise ValueError("confidence 是 NaN")
    return max(0.0, min(1.0, value))


def _clean_objections(raw: Any) -> list[str]:
    """清洗模型的反对意见：丢非字符串、去重、单条截断、最多 3 条。

    单个字符串按「一条」处理而不是整卡回退：模型写成 ``"objections": "..."``
    很常见，为格式丢掉一条有价值的质疑不划算；真正结构坏掉（对象/数字）才回退。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        raise ValueError("objections 不是列表")

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue  # 模型偶尔塞对象/数字进来，丢掉这一条就行
        text = " ".join(item.split()).strip()
        if len(text) > MAX_OBJECTION_CHARS:
            text = text[:MAX_OBJECTION_CHARS]
        if not text or text in seen or text.lower() in _EMPTY_OBJECTIONS:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_LLM_OBJECTIONS:
            break
    return out


def _merge_objections(rule: list[str], extra: list[str]) -> list[str]:
    """规则版的排在前面，模型补的跟在后面；去重后最多 4 条。"""
    out: list[str] = []
    seen: set[str] = set()
    for text in list(rule or []) + list(extra or []):
        if not isinstance(text, str):
            continue
        key = text.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= MAX_MERGED_OBJECTIONS:
            break
    return out


def _verdict_of(confidence: float) -> str:
    """和 ``critic.critique`` 完全同一套阈值，避免两处判定漂移。"""
    if confidence < REJECT_BELOW:
        return "reject"
    if confidence < SUPPORT_MIN:
        return "weaken"
    return "support"


def _apply(verdict: Verdict, data: Any) -> Verdict:
    """把模型输出并进规则版结论；这一步是「只降不升」的唯一落点。

    模型给的 ``verdict`` 字段不参与判定：档位永远由压下来的 confidence 重算。
    否则会出现「置信度 0.6 却驳回」这种自相矛盾的展示，而模型对档位的措辞
    远不如它对概率的判断可靠。非法 verdict（如 "maybe"）因此也不会报错。

    结构不对（不是对象 / confidence 不是数字 / objections 不是列表）抛 ValueError，
    由调用方把这张卡退回规则版。
    """
    if not isinstance(data, dict):
        raise ValueError("返回不是 JSON 对象")

    model_confidence = _to_confidence(data.get("confidence"))
    objections = _clean_objections(data.get("objections"))

    # 模型天然偏苛刻：对每张卡都给 0.5 附近的分，完全照它走会把所有卡压成「存疑」，
    # 排序也就没意义了。所以单张卡最多压 20%，它的主要价值在于「提出反方意见」。
    floor = float(verdict.confidence_after) * LLM_CONFIDENCE_FLOOR
    after = min(float(verdict.confidence_after), max(model_confidence, floor))
    before = float(verdict.confidence_before)
    return replace(
        verdict,
        verdict=_verdict_of(after),
        confidence_after=after,
        # 规则版用 before*factor 得到 after；这里保持同一口径反向写回。
        factor=(after / before) if before > 0 else 0.0,
        objections=_merge_objections(verdict.objections, objections),
        source="llm",
    )


def _pick_card(cards: list[dict], index: int, verdict: Verdict) -> dict[str, Any] | None:
    """先按 card_id 找卡，找不到再按位置兜底；都对不上说明调用方给错了数据。"""
    for card in cards:
        if not isinstance(card, dict):
            continue
        if verdict.card_id and str(card.get("card_id") or "") == verdict.card_id:
            return card
    if 0 <= index < len(cards) and isinstance(cards[index], dict):
        return cards[index]
    return None


def review_cards(
    cards: list[dict],
    verdicts: list[Verdict],
    *,
    llm=None,
    max_cards: int = LLM_CRITIC_CARDS,
    budget_ms: int = LLM_CRITIC_BUDGET_MS,
) -> tuple[list[Verdict], dict]:
    """对已排序的诊断卡做 LLM 反方复查，只降不升，任何失败静默回退。

    ``cards`` 与 ``verdicts`` 同序（``critic.critique()`` 的输出）。返回新的
    verdict 列表，入参不被修改；``meta`` 说明这次复查发生了什么。
    """
    started = time.perf_counter()
    base = list(verdicts or [])
    meta: dict[str, Any] = {
        "requested": False,
        "used": 0,
        "degraded": False,
        "reason": "未启用 LLM 复查",
        "elapsed_ms": 0.0,
        "cards": [],
        # 下面几个是调试用的附加字段，调用方可以不用。
        "calls": 0,
        "max_cards": int(max_cards),
        "budget_ms": float(budget_ms),
    }

    if llm is None:
        # 没启用不是错误：规则版本来就是完整可用的结果。
        meta["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        return base, meta

    meta["requested"] = True
    reasons: list[str] = []

    for index, verdict in enumerate(base):
        if index >= max_cards:
            break
        if verdict.verdict == "reject":
            # 已经驳回的卡不必再问：模型最好的结果也只能持平。
            continue
        # 预算只能在「发起调用前」判断：chat_json 是同步阻塞的，单次调用一旦发出
        # 就没法中途掐断，所以这里的语义是「不再开始新的一张」。
        if budget_ms - (time.perf_counter() - started) * 1000.0 <= 0:
            reasons.append(f"第 {index + 1} 张：超出 {budget_ms}ms 复查预算，已停止")
            break

        card = _pick_card(cards, index, verdict)
        if card is None:
            reasons.append(f"第 {index + 1} 张：找不到对应卡片")
            continue

        meta["calls"] += 1
        try:
            data = llm.chat_json(
                _build_messages(_compact_facts(card, verdict)),
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
            )
            base[index] = _apply(verdict, data)
        except LLMError as exc:
            # 网关级失败（无 key、超时、JSON 解析不出来）：后面几张大概率一样，
            # 继续逐张重试只会把 4 秒预算烧光，直接停。
            reasons.append(f"第 {index + 1} 张：{type(exc).__name__}: {exc}")
            break
        except Exception as exc:  # noqa: BLE001 - 单卡任何异常都只能回退
            reasons.append(f"第 {index + 1} 张：{type(exc).__name__}: {exc}")
            continue
        else:
            meta["used"] += 1
            meta["cards"].append(verdict.card_id)

    meta["degraded"] = bool(reasons)
    if reasons:
        meta["reason"] = "；".join(reasons)
    elif meta["used"]:
        meta["reason"] = f"已复查 {meta['used']} 张卡"
    else:
        meta["reason"] = "没有可复查的卡"
    meta["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return base, meta
