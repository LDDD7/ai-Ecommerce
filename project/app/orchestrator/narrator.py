"""报告叙述器：让 LLM 在「事实」约束下重写模板报告，校验不过就整段回退。

设计原则：

- 模板永远是可用的兜底；LLM 只负责组织语言，不负责产生数字。
- 数字校验是硬闸门：宁可回退模板，也不把编造的数字发给用户。
- 不 import ``app.core.llm``（该模块并行开发中），只按 duck typing 调
  ``llm.chat(...)``，失败一律回退。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 数字校验的失败关：输出里出现任何一个事实中不存在的数字，整段回退。
_MIN_CHARS = 200
_MAX_CHARS = 900
_REL_TOL = 0.02

# 参考写法只取模板的前几段：全量 15 段模板本身就要 1200+ token，
# 但风格示范不需要把所有卡片和动作都塞进去。
_REFERENCE_MAX_PARAGRAPHS = 4
_REFERENCE_MAX_CHARS = 600

# 只抽「像数字」的片段：可带负号、千分位、小数与百分号。
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*%?")

SYSTEM_PROMPT = (
    "你是电商店铺经营分析师，中文写作。\n"
    "硬规则：\n"
    "1. 只能引用【事实】里出现过的数字，不得计算新数字，也不得四舍五入到看不出"
    "与事实的对应关系。\n"
    "2. 不要输出 markdown 表格、代码块或标题符号（#、|、```）。正文写 200~420 字，"
    "分 3~4 段自然段，每段不超过 3 句；超过 420 字会被整段丢弃。\n"
    "3. 提到动作时只能点名【建议动作】里给出的策略卡标题，不得编造新的动作或卡片编号；"
    "若那里是「（无）」，就干脆不要提任何动作。\n"
    "4. 【事实】里没有的内容就不要说。\n"
    "5. 结尾不要加「总结」之类的套话。"
)


@dataclass(slots=True)
class Narrative:
    """一次叙述的结果：段落、来源、回退原因与校验快照。"""

    paragraphs: list[str]
    source: str
    reason: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "paragraphs": list(self.paragraphs),
            "source": self.source,
            "reason": self.reason,
            "checks": dict(self.checks),
        }


def _chars(paragraphs: list[str]) -> int:
    """字数：段落正文长度之和，不含段落分隔符。"""
    return sum(len(p) for p in paragraphs)


def _template_result(
    template_paragraphs: list[str],
    reason: str | None,
    *,
    numbers_ok: bool = True,
    bad_numbers: list[str] | None = None,
    chars: int | None = None,
) -> Narrative:
    paragraphs = list(template_paragraphs)
    return Narrative(
        paragraphs=paragraphs,
        source="template",
        reason=reason,
        checks={
            "numbers_ok": numbers_ok,
            "bad_numbers": list(bad_numbers or []),
            "chars": _chars(paragraphs) if chars is None else chars,
        },
    )


def _clean_paragraphs(text: str) -> list[str]:
    """按换行切段，丢掉空行、标题、表格行与代码块（含块内内容）。"""
    paragraphs: list[str] = []
    in_code = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line:
            continue
        # 标题、表格行（含不带开头竖线的写法）；表格分隔行（如 --- | ---）也丢掉。
        if line.startswith("#") or "|" in line:
            continue
        if set(line) <= set("-:| "):
            continue
        paragraphs.append(line)
    return paragraphs


def _to_float(raw: str) -> float | None:
    cleaned = (
        raw.strip()
        .replace(",", "")
        .replace("，", "")
        .replace("%", "")
        .replace("％", "")
        .replace("元", "")
        .strip()
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def _iter_numbers(value: Any) -> list[float]:
    """递归收集事实里的数字。

    除了 JSON 里的 int/float，也接受字符串中出现的数字：日期、摘要、SKU 编号
    这些内容同样属于「事实」，模型复述它们不应被误判为编造。
    """
    found: list[float] = []
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, (int, float)):
        number = float(value)
        if number == number and number not in (float("inf"), float("-inf")):
            found.append(number)
        return found
    if isinstance(value, str):
        for match in _NUMBER_RE.findall(value):
            parsed = _to_float(match)
            if parsed is not None:
                found.append(parsed)
        return found
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_iter_numbers(item))
        return found
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found.extend(_iter_numbers(item))
    return found


def _number_forms(number: float) -> set[str]:
    """一个事实数字的可接受写法。"""
    forms = {
        str(number),
        f"{number:.1f}",
        f"{number:.2f}",
        f"{number:,.0f}",
        f"{abs(number) * 100:.1f}%",
    }
    text = str(number)
    if text.endswith(".0"):
        forms.add(text[:-2])
    if float(number).is_integer():
        forms.add(str(int(number)))
    # 正文常说「下滑 1,152,601 元」「降 53.2%」，写的是绝对值而不是负号；
    # 同一事实的绝对值写法也应放行，否则真链路会因符号习惯被误杀。
    magnitude = abs(number)
    forms.update(
        {
            str(magnitude),
            f"{magnitude:.1f}",
            f"{magnitude:.2f}",
            f"{magnitude:,.0f}",
        }
    )
    if magnitude.is_integer():
        forms.add(str(int(magnitude)))
    return forms


def _allowed_numbers(*sources: Any) -> list[float]:
    allowed: list[float] = []
    for source in sources:
        for number in _iter_numbers(source):
            for form in _number_forms(number):
                parsed = _to_float(form)
                if parsed is not None:
                    allowed.append(parsed)
    # 去重，避免候选数字逐个扫重复值。
    return list(dict.fromkeys(allowed))


def _matches(candidate: float, allowed: list[float]) -> bool:
    for number in allowed:
        if candidate == number:
            return True
        scale = abs(number)
        if scale > 0 and abs(candidate - number) <= _REL_TOL * scale:
            return True
    return False


def _bad_numbers(paragraphs: list[str], allowed: list[float]) -> list[str]:
    bad: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        for raw in _NUMBER_RE.findall(paragraph):
            value = _to_float(raw)
            if value is None or _matches(value, allowed):
                continue
            if raw not in seen:
                seen.add(raw)
                bad.append(raw)
    return bad


def _intent_facts(intent: Any) -> dict[str, Any]:
    if intent is None:
        return {}
    if isinstance(intent, dict):
        return dict(intent)
    data: dict[str, Any] = {}
    for name in ("kind", "window_days", "as_of", "sku", "styles", "metrics"):
        if hasattr(intent, name):
            value = getattr(intent, name)
            if value is not None:
                data[name] = value
    return data


def _action_lines(playbook: dict[str, Any] | None) -> str:
    lines: list[str] = []
    for item in (playbook or {}).get("items") or []:
        titles = [a.get("title") for a in item.get("actions") or [] if a.get("title")]
        if titles:
            lines.append(f"- {item.get('code', '')} {item.get('title', '')}：{'；'.join(titles)}")
    return "\n".join(lines)


def _slim_evidence(evidence: Any) -> Any:
    """证据只保留校验与叙述真正需要的字段；字符串原样保留。"""
    if not isinstance(evidence, dict):
        return evidence
    keys = ("metric", "dim_value", "baseline", "current", "delta_pct")
    slim = {key: evidence[key] for key in keys if evidence.get(key) is not None}
    return slim or None


def _compact_facts(
    overview: dict[str, Any] | None,
    diagnose: dict[str, Any] | None,
    playbook: dict[str, Any] | None,
    intent_facts: dict[str, Any],
) -> dict[str, Any]:
    """把工具结果压成提示词事实：只保留会被正文引用的字段。

    真链路里完整 JSON 有 1.5 万 token，既拖慢生成又容易让模型抄表格；
    这里按「总览 6 个 KPI + 健康分、诊断 2 段摘要 + 前 3 张卡 + 每卡前 2 条
    证据、建议动作最多 6 条」裁剪，数字校验仍以裁剪后的事实为准。
    """
    overview = overview if isinstance(overview, dict) else {}
    diagnose = diagnose if isinstance(diagnose, dict) else {}
    playbook = playbook if isinstance(playbook, dict) else {}

    facts: dict[str, Any] = {}

    ov: dict[str, Any] = {}
    if overview.get("as_of") is not None:
        ov["as_of"] = overview["as_of"]
    if intent_facts.get("window_days") is not None:
        ov["window_days"] = intent_facts["window_days"]

    kpis: list[dict[str, Any]] = []
    for kpi in overview.get("kpis") or []:
        if not isinstance(kpi, dict):
            continue
        item = {
            key: kpi[key]
            for key in ("name", "value", "delta_pct")
            if kpi.get(key) is not None
        }
        if item:
            kpis.append(item)
        if len(kpis) >= 6:
            break
    if kpis:
        ov["kpis"] = kpis

    health = overview.get("health")
    if isinstance(health, dict):
        slim_health = {
            key: health[key]
            for key in ("score", "grade")
            if health.get(key) is not None
        }
        if slim_health:
            ov["health"] = slim_health
    if ov:
        facts["overview"] = ov

    dg: dict[str, Any] = {}
    gmv = diagnose.get("gmv")
    if isinstance(gmv, dict) and gmv.get("summary"):
        dg["gmv"] = {"summary": gmv["summary"]}
    funnel = diagnose.get("funnel")
    if isinstance(funnel, dict) and funnel.get("summary"):
        dg["funnel"] = {"summary": funnel["summary"]}

    cards: list[dict[str, Any]] = []
    for card in (diagnose.get("cards") or [])[:3]:
        if not isinstance(card, dict):
            continue
        scope = card.get("scope") if isinstance(card.get("scope"), dict) else {}
        slim_card: dict[str, Any] = {}
        for key, value in (
            ("code", card.get("code")),
            ("title", card.get("title")),
            ("dim_value", scope.get("dim_value")),
            ("severity", card.get("severity")),
            ("gmv_impact", card.get("gmv_impact")),
            ("narrative", card.get("narrative")),
        ):
            if value is not None:
                slim_card[key] = value
        evidence = [
            _slim_evidence(item)
            for item in (card.get("evidence") or [])[:2]
        ]
        evidence = [item for item in evidence if item]
        if evidence:
            slim_card["evidence"] = evidence
        if slim_card:
            cards.append(slim_card)
    if cards:
        dg["cards"] = cards
    if dg:
        facts["diagnose"] = dg

    items: list[dict[str, Any]] = []
    remaining = 6
    for item in playbook.get("items") or []:
        if remaining <= 0:
            break
        if not isinstance(item, dict):
            continue
        actions: list[dict[str, Any]] = []
        for action in item.get("actions") or []:
            if remaining <= 0:
                break
            if not isinstance(action, dict):
                continue
            effect = action.get("expected_effect")
            effect = effect if isinstance(effect, dict) else {}
            slim_action: dict[str, Any] = {}
            if action.get("title"):
                slim_action["title"] = action["title"]
            if action.get("risk") is not None:
                slim_action["risk"] = action["risk"]
            if effect.get("metric") is not None:
                slim_action["metric"] = effect["metric"]
            if effect.get("range") is not None:
                slim_action["range"] = effect["range"]
            if slim_action:
                actions.append(slim_action)
                remaining -= 1
        if actions:
            slim_item: dict[str, Any] = {}
            if item.get("code"):
                slim_item["code"] = item["code"]
            if item.get("title"):
                slim_item["title"] = item["title"]
            slim_item["actions"] = actions
            items.append(slim_item)
    if items:
        facts["playbook"] = {"items": items}

    return facts


def _reference_paragraphs(paragraphs: list[str]) -> list[str]:
    """参考写法只保留前几段，避免模板本身成为提示词的大头。"""
    selected: list[str] = []
    chars = 0
    for paragraph in paragraphs:
        if len(selected) >= _REFERENCE_MAX_PARAGRAPHS:
            break
        if selected and chars + len(paragraph) > _REFERENCE_MAX_CHARS:
            break
        selected.append(paragraph)
        chars += len(paragraph)
    return selected


def _build_messages(
    facts: dict[str, Any],
    template_paragraphs: list[str],
    intent_facts: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    facts_json = json.dumps(facts, ensure_ascii=False, indent=2, default=str)
    actions = _action_lines(facts.get("playbook")) or "（无）"
    reference = "\n".join(f"{i}. {p}" for i, p in enumerate(template_paragraphs, 1))
    question = ""
    if intent_facts:
        bits = "；".join(f"{key}={value}" for key, value in intent_facts.items())
        question = f"【本次问题】\n{bits}\n\n"
    # 没有诊断卡 / 没有动作时，静态的「只写前 2 张卡」「必须点名动作」会把模型逼去编造，
    # 所以这两句按事实动态改写。
    cards = ((facts.get("diagnose") or {}).get("cards")) or []
    if cards:
        card_rule = "只写影响最大的 2 张卡，第三张及以后不要写，不要逐张卡展开。"
    else:
        card_rule = "本次没有诊断卡，不要提卡片、根因编号或「第 N 张卡」。"
    action_rule = (
        "提到动作时必须点名事实里的策略卡标题，不得编造新的动作。"
        if actions != "（无）"
        else "本次没有可用策略卡，不要提具体动作，结尾建议人工确认即可。"
    )
    user = (
        question
        + "【事实】\n"
        f"{facts_json}\n\n"
        "【建议动作】\n"
        f"{actions}\n\n"
        "【参考写法】\n"
        f"{reference}\n\n"
        "请基于【事实】写 3 段中文经营分析，全文 300~380 字（最多 400 字），"
        "每段 2~3 句；只使用事实中出现过的数字；"
        f"{card_rule}{action_rule}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def narrate(
    intent: Any,
    overview: dict[str, Any] | None,
    diagnose: dict[str, Any] | None,
    playbook: dict[str, Any] | None,
    *,
    template_paragraphs: list[str],
    llm: Any = None,
    max_tokens: int = 500,
) -> Narrative:
    """用 LLM 重写模板段落；任何校验失败都整段回退模板。"""
    template = list(template_paragraphs or [])

    if llm is None:
        return _template_result(template, "llm 未启用")

    intent_facts = _intent_facts(intent)
    facts = _compact_facts(overview, diagnose, playbook, intent_facts)
    reference = _reference_paragraphs(template)

    try:
        result = llm.chat(
            _build_messages(facts, reference, intent_facts),
            max_tokens=max_tokens,
        )
        text = result.text
    except Exception as exc:  # noqa: BLE001 - 外部 LLM 的任何失败都只能回退
        return _template_result(template, f"llm 调用失败：{type(exc).__name__}")

    if not isinstance(text, str):
        return _template_result(template, "llm 调用失败：TypeError")

    paragraphs = _clean_paragraphs(text)
    chars = _chars(paragraphs)
    allowed = _allowed_numbers(facts, intent_facts, reference)
    bad = _bad_numbers(paragraphs, allowed)

    if bad:
        return _template_result(
            template,
            f"数字校验失败：{bad[:3]}",
            numbers_ok=False,
            bad_numbers=bad,
            chars=chars,
        )

    if chars < _MIN_CHARS:
        return _template_result(template, f"输出过短：{chars} 字（<{_MIN_CHARS}）", chars=chars)
    if chars > _MAX_CHARS:
        return _template_result(template, f"输出过长：{chars} 字（>{_MAX_CHARS}）", chars=chars)

    return Narrative(
        paragraphs=paragraphs,
        source="llm",
        reason=None,
        checks={"numbers_ok": True, "bad_numbers": [], "chars": chars},
    )
