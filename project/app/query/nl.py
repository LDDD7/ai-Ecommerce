"""自然语言 → QuerySpec。

两条路：

1. **规则解析**（无 key / LLM 失败时）：复用编排层的意图抽取（时间窗、指标、款式、渠道、
   SKU），再补一层「按天 / 按款式 / 按渠道」这类维度词。它不是 NL2SQL，只是把常见问法
   映射成结构化查询——但好处是确定、可测、零成本。
2. **LLM 解析**（有 key 时）：把「24 个指标 + 15 个维度 + 约束」喂给模型，要求只输出
   QuerySpec 的 JSON；拿到后一律走 `QuerySpec` 校验，校验失败就整段回退规则解析。

无论哪条路，最终都只产出 `QuerySpec`（结构化），SQL 由语义层编译器生成——模型没有写
SQL 的机会，这是 §6 安全网关的前提。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.metrics.spec import MAX_DIMS, MAX_FILTERS, MAX_METRICS, MAX_ROWS, QuerySpec, load_catalog
from app.orchestrator.intents import METRIC_WORDS, parse_intent

# 「要不要按维度拆」的中文说法。顺序有意义：先匹配更具体的词。
DIM_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("按天", ("按天", "每天", "逐日", "按日", "daily")),
    ("按周", ("按周", "每周", "weekly")),
    ("按月", ("按月", "每月", "monthly")),
    ("款式", ("款式", "风格", "style", "按款")),
    ("渠道", ("渠道", "来源", "channel")),
    ("商品", ("商品", "单品", "sku", "爆款", "按货")),
    ("价格带", ("价格带", "价位", "price_band")),
    ("面料", ("面料", "材质", "fabric")),
    ("颜色", ("颜色", "色系", "color")),
    ("供应商", ("供应商", "供货", "supplier")),
    ("是否促销", ("促销日", "是否促销", "is_promo")),
)

DIM_MAP = {
    "按天": "dt",
    "按周": "week",
    "按月": "month",
    "款式": "style",
    "渠道": "channel_id",
    "商品": "sku_id",
    "价格带": "price_band",
    "面料": "fabric",
    "颜色": "color",
    "供应商": "supplier",
    "是否促销": "is_promo",
}

DEFAULT_METRICS = ("gmv", "paid_orders")

# 「近 14 天」「最近 30 天」这类显式窗口，优先于调用方给的默认值。
_WINDOW_NUM_RE = re.compile(r"(?:近|最近|过去|前)\s*(\d{1,2})\s*(?:天|日)")
_WINDOW_WORD_RE = re.compile(r"(上个月|上月|这个月|本月|最近一个月|近一个月|一个月|这周|本周|上周|这季度|本季度)")


def _window_days_from_text(question: str) -> int | None:
    m = _WINDOW_NUM_RE.search(question)
    if m:
        return max(1, min(int(m.group(1)), 90))
    m = _WINDOW_WORD_RE.search(question)
    if m:
        word = m.group(1)
        if "季度" in word:
            return 90
        if "月" in word:
            return 30
        return 7
    return None

PLANNER_SYSTEM = """你是电商数据查询规划器。把用户问题翻译成 QuerySpec JSON，只输出 JSON。

可用指标（name: 中文名）：
{metrics}

可用维度：
{dims}

规则：
1. metrics 取 1~{max_metrics} 个，必须是上面列出的 name；用户没说时默认 ["gmv","paid_orders"]。
2. dimensions 取 0~{max_dims} 个，只能从可用维度里选；用户说「按天/每天」→ "dt"，「按款式」→ "style"，「按渠道」→ "channel_id"。
3. window_days 是整数 1~{max_days}，从「近 14 天 / 上周 / 这个月」这类说法推；没说就 7。
4. filters 最多 {max_filters} 条，形如 {{"field":"style","op":"=","value":"甜美"}}；op 只能是 = != > >= < <= in not in between like。
5. limit 整数 1~{max_rows}，默认 200。
6. 只输出一个 JSON 对象：{{"metrics":[...],"dimensions":[...],"window_days":7,"filters":[...],"limit":200}}。
7. 不要输出 SQL、不要解释、不要 markdown 代码块。"""


@dataclass(slots=True)
class NLResult:
    spec: dict[str, Any]
    source: str  # "llm" | "rule"
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    question: str = ""
    # 解析出来的窗口长度（天）。放在结果上而不是塞进 spec：
    # QuerySpec 现在 extra="forbid"，多一个字段整条查询就会被判非法。
    window_days: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "source": self.source,
            "warnings": list(self.warnings),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "question": self.question,
            "window_days": self.window_days,
        }


def _catalog_names() -> tuple[list[str], list[str]]:
    """指标名与「至少一个指标支持」的维度集合（`dims` 是维度组名，展开后才能比对）。"""
    cat = load_catalog()
    names: list[str] = []
    dims: set[str] = set()
    for m in cat.public():
        names.append(m["name"])
        dims.update(m["dimensions"])
    return sorted(names), sorted(dims)


def _pick_dimensions(question: str) -> list[str]:
    picked: list[str] = []
    for label, words in DIM_WORDS:
        if any(w.lower() in question.lower() for w in words):
            dim = DIM_MAP[label]
            if dim not in picked:
                picked.append(dim)
    return picked[:MAX_DIMS]


def _pick_metrics(question: str, intent_metrics: list[str]) -> list[str]:
    names, _ = _catalog_names()
    picked = [m for m in intent_metrics if m in names]
    # 「转化」「退款」这类词 intents 已覆盖；再兜一层中文名的直接命中。
    cat = load_catalog()
    for m in cat.metrics.values():
        if m.display and m.display in question and m.name not in picked:
            picked.append(m.name)
    return picked[:MAX_METRICS]


def _rule_spec(question: str, *, window_days: int, as_of: str | None) -> tuple[dict[str, Any], list[str]]:
    intent = parse_intent(question)
    warnings: list[str] = []
    metrics = _pick_metrics(question, list(intent.metrics))
    if not metrics:
        metrics = list(DEFAULT_METRICS)
        warnings.append("没听出要查哪个指标，先用「成交额 + 支付订单数」")
    dims = _pick_dimensions(question)
    filters: list[dict[str, Any]] = []
    for style in intent.styles:
        filters.append({"field": "style", "op": "=", "value": style})
    for channel in intent.channels:
        filters.append({"field": "channel_id", "op": "=", "value": channel})
    if intent.sku:
        filters.append({"field": "sku_id", "op": "=", "value": intent.sku})
    days = _window_days_from_text(question) or window_days or intent.window_days
    spec: dict[str, Any] = {
        "metrics": metrics,
        "dimensions": dims,
        "window_days": days,
        "filters": filters[:MAX_FILTERS],
        "limit": 200,
    }
    if as_of:
        spec["as_of"] = as_of
    return spec, warnings


def _build_spec(raw: dict[str, Any], *, question: str, window_days: int, as_of: str | None) -> tuple[dict[str, Any], list[str]]:
    """把模型输出规整成合法的 QuerySpec 形状（这里只做形状，不做 SQL）。"""
    names, dims = _catalog_names()
    warnings: list[str] = []
    metrics = [m for m in (raw.get("metrics") or []) if m in names][:MAX_METRICS]
    if not metrics:
        metrics = list(DEFAULT_METRICS)
        warnings.append("模型没给出可用指标，回退到成交额 + 支付订单数")
    picked_dims = [d for d in (raw.get("dimensions") or []) if d in dims][:MAX_DIMS]
    try:
        days = int(raw.get("window_days") or window_days)
    except (TypeError, ValueError):
        days = window_days
        warnings.append("模型给的窗口不是整数，回退默认窗口")
    days = max(1, min(days, 90))
    filters = []
    for f in (raw.get("filters") or [])[:MAX_FILTERS]:
        if isinstance(f, dict) and f.get("field") and f.get("op") is not None and "value" in f:
            filters.append({"field": str(f["field"]), "op": str(f["op"]), "value": f["value"]})
    try:
        limit = int(raw.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, MAX_ROWS))
    spec: dict[str, Any] = {
        "metrics": metrics,
        "dimensions": picked_dims,
        "window_days": days,
        "filters": filters,
        "limit": limit,
    }
    if as_of:
        spec["as_of"] = as_of
    return spec, warnings


def _validate(spec: dict[str, Any], *, as_of: str | None) -> dict[str, Any]:
    """用真正的 QuerySpec 校验一遍，顺带补上 time 区间（前端拿到就能直接跑）。"""
    from datetime import date, timedelta

    days = int(spec.get("window_days") or 7)
    end = date.fromisoformat(as_of) if as_of else date.today()
    start = end - timedelta(days=days - 1)
    payload = {
        "metrics": spec["metrics"],
        "dimensions": spec.get("dimensions") or [],
        "filters": spec.get("filters") or [],
        "time": {"start": start.isoformat(), "end": end.isoformat()},
        "limit": spec.get("limit") or 200,
    }
    model = QuerySpec(**payload)
    # 只回严格 spec：window_days 不塞进 spec（QuerySpec 现在 extra="forbid"，
    # 多一个字段整条请求就会被判非法），窗口信息已由 time 区间表达。
    return model.model_dump(mode="json")


def parse_question(
    question: str,
    *,
    as_of: str | None = None,
    window_days: int = 7,
    llm: Any = None,
    use_llm: bool = True,
) -> NLResult:
    """把一句中文问题解析成 QuerySpec。`llm` 可注入（测试用 FakeLLM）。"""
    t0 = time.perf_counter()
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    warnings: list[str] = []
    source = "rule"
    spec: dict[str, Any] | None = None

    if use_llm:
        client = llm
        if client is None:
            try:
                from app.core import runtime
                from app.core.llm import get_client

                if runtime.llm_active() == "real":
                    client = get_client()
            except Exception:  # 网关没装 / 没配 key：直接走规则
                client = None
        if client is not None:
            names, dims = _catalog_names()
            prompt = PLANNER_SYSTEM.format(
                metrics="\n".join(f"- {m.name}" for m in load_catalog().metrics.values()),
                dims="、".join(dims),
                max_metrics=MAX_METRICS,
                max_dims=MAX_DIMS,
                max_filters=MAX_FILTERS,
                max_days=90,
                max_rows=MAX_ROWS,
            )
            try:
                raw = client.chat_json(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": question},
                    ],
                    max_tokens=500,
                )
                if not isinstance(raw, dict):
                    raise ValueError("模型返回的不是对象")
                spec, w = _build_spec(raw, question=question, window_days=window_days, as_of=as_of)
                warnings.extend(w)
                source = "llm"
            except Exception as exc:  # 任何失败都回退，不让查数挂掉
                warnings.append(f"模型解析失败（{type(exc).__name__}: {exc}），已回退规则解析")
                spec = None

    if spec is None:
        spec, w = _rule_spec(question, window_days=window_days, as_of=as_of)
        warnings.extend(w)

    try:
        final = _validate(spec, as_of=as_of)
    except Exception as exc:
        # 规则解析也可能撞上（比如维度不被所选指标支持）——退到最小可跑查询
        warnings.append(f"查询校验未通过（{exc}），已退到成交额按天")
        final = _validate({"metrics": ["gmv"], "dimensions": ["dt"], "filters": [], "limit": 200}, as_of=as_of)
    span_days = 0
    if final.get("time"):
        start = date.fromisoformat(final["time"]["start"])
        end = date.fromisoformat(final["time"]["end"])
        span_days = (end - start).days + 1
    return NLResult(
        spec=final,
        source=source,
        warnings=warnings,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        question=question,
        window_days=span_days,
    )


__all__ = ["NLResult", "parse_question", "DIM_MAP", "PLANNER_SYSTEM"]
