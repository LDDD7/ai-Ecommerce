"""把素材属性折算成诊断卡上的证据与发现（material_health）。

输入是 `image_feature.features_json` 的 dict、可选的 `compare()` 结果；
输出直接能塞进诊断卡 `card["material"]`。本模块**不实例化**
`app.vision.schema.MaterialFeatures`，只读 dict 字段——视觉检查器
（inspector）与这里并行开发，避免两边同时改 schema 的类定义而互相踩脚。

问题码沿用 `schema.ISSUE_SPECS` 的英文码；库里历史数据可能存中文 flag，
这里用 `CN_TO_CODE` 归一化，未知 flag 直接忽略（不猜）。
"""

from __future__ import annotations

from typing import Any, Iterable

from app.vision.schema import ISSUE_SPECS

MATERIAL_METRIC = "material_health"

# 中文 flag -> 英文问题码。schema 里暂时没有这张表，先放在本模块；
# 若 schema 之后补上 CN_TO_CODE，行为仍以这里的映射为准，保持离线可测。
CN_TO_CODE: dict[str, str] = {
    "无尺码表": "NO_SIZE_CHART",
    "文字占比偏高": "TEXT_HEAVY",
    "纯白底无场景": "PLAIN_WHITE",
    "有水印": "WATERMARK",
    "拼接图": "COLLAGE",
    "无模特上身": "NO_MODEL",
    "无模特": "NO_MODEL",
}

# 每个问题码的扣分权重；全中正好扣到 0.0（下限兜底）。
CODE_WEIGHTS: dict[str, float] = {
    "NO_SIZE_CHART": 0.25,
    "TEXT_HEAVY": 0.20,
    "PLAIN_WHITE": 0.15,
    "WATERMARK": 0.15,
    "COLLAGE": 0.10,
    "NO_MODEL": 0.15,
}

HIGH_SEVERITY = {"NO_SIZE_CHART", "TEXT_HEAVY"}

# 问题码 -> compare() 里对应的 gap 字段，用来把同款式中位差距拼进证据。
GAP_FIELD: dict[str, str] = {
    "NO_SIZE_CHART": "size_chart_present",
    "TEXT_HEAVY": "text_coverage",
    "PLAIN_WHITE": "plain_white",
    "WATERMARK": "watermark",
    "COLLAGE": "collage",
    "NO_MODEL": "model_present",
}

# 只有这两个根因与素材有因果关系，别的卡不硬蹭。
MATERIAL_ROOT_CAUSES = ("R2", "R4")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "有", "是")
    return bool(value)


def normalize_flags(flags: Iterable[Any] | None) -> list[str]:
    """中文/英文 flag 归一成 `ISSUE_SPECS` 里的英文码，去重保序。"""
    out: list[str] = []
    for raw in flags or []:
        text = str(raw).strip()
        if not text:
            continue
        code = CN_TO_CODE.get(text, text)
        if code in ISSUE_SPECS and code not in out:
            out.append(code)
    return out


def material_score(features: dict[str, Any]) -> float:
    """素材健康分：1.0 减去每个问题码的权重，下限 0.0。"""
    penalty = sum(CODE_WEIGHTS.get(code, 0.0) for code in normalize_flags(features.get("quality_flags")))
    return round(max(0.0, 1.0 - penalty), 4)


def findings(
    features: dict[str, Any],
    *,
    compare_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把 `quality_flags` 逐条折成带根因/动作/同行差距的发现。"""
    gap_text: dict[str, str] = {}
    if compare_result:
        for gap in compare_result.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
            field = gap.get("field")
            text = gap.get("text")
            if field and text:
                gap_text.setdefault(str(field), str(text))

    out: list[dict[str, Any]] = []
    for code in normalize_flags(features.get("quality_flags")):
        spec = ISSUE_SPECS[code]
        field = GAP_FIELD.get(code, "")
        out.append(
            {
                "code": code,
                "label": spec["label"],
                "detail": spec["detail"],
                "root_causes": list(spec.get("root_causes") or []),
                "action": spec.get("action", ""),
                "severity": "high" if code in HIGH_SEVERITY else "medium",
                "peer_gap": gap_text.get(field, "") if field else "",
            }
        )
    return out


def _evidence_text(features: dict[str, Any], role: str) -> str:
    """与 `MaterialFeatures.to_evidence()` 同风格的一句中文，但只读 dict。"""
    bits = [f"{role} 图"]
    coverage = _as_float(features.get("text_coverage"))
    if coverage:
        bits.append(f"文字占比 {coverage * 100:.0f}%")
    bits.append("有模特" if _as_bool(features.get("model_present")) else "无模特")
    scene = str(features.get("scene") or "").strip()
    if scene:
        bits.append(f"场景：{scene}")
    bits.append("有尺码表" if _as_bool(features.get("size_chart_present")) else "无尺码表")
    if _as_bool(features.get("watermark")):
        bits.append("有水印")
    if _as_bool(features.get("collage")):
        bits.append("拼接图")
    return "，".join(bits)


def _to_evidence(
    features: dict[str, Any],
    *,
    sku_id: str,
    role: str,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    source = str(features.get("source") or "mock")
    confidence = round(min(1.0, max(0.0, _as_float(features.get("confidence"), 1.0))), 3)
    return {
        "kind": "material",
        "sku_id": sku_id,
        "role": role,
        "text": _evidence_text(features, role),
        "issues": issues,
        "source": source,
        "confidence": confidence,
    }


def _root_codes(card: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for key in ("code", "root_cause"):
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            codes.add(value.strip())
    for cand in card.get("candidates") or []:
        if isinstance(cand, dict):
            value = cand.get("code")
        else:
            value = getattr(cand, "code", None)
        if isinstance(value, str) and value.strip():
            codes.add(value.strip())
    return codes


def attach_to_cards(
    cards: list[dict[str, Any]],
    items: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """给 R2/R4 的 SKU 卡挂素材分、发现与证据。

    `items` 形如 `{sku_id: {"features": {...}, "compare": {...} | None}}`。
    只处理 `scope.dim == "sku_id"` 的卡；其它维度的卡不计数、不改动。
    """
    attached = 0
    skipped = 0
    skus: list[str] = []

    for card in cards:
        if not isinstance(card, dict):
            continue
        scope = card.get("scope")
        if not isinstance(scope, dict) or scope.get("dim") != "sku_id":
            continue
        sid = str(scope.get("dim_value") or "")
        if (
            not sid
            or sid not in items
            or not (_root_codes(card) & set(MATERIAL_ROOT_CAUSES))
        ):
            skipped += 1
            continue

        item = items.get(sid) or {}
        features = item.get("features") or {}
        if not isinstance(features, dict):
            features = {}
        cmp = item.get("compare")
        cmp_result = cmp if isinstance(cmp, dict) else None
        role = str(features.get("role") or "main")

        issue_list = findings(features, compare_result=cmp_result)
        card["material"] = {
            "score": material_score(features),
            "findings": issue_list,
            "evidence": _to_evidence(features, sku_id=sid, role=role, issues=issue_list),
            "compare_summary": str(cmp_result.get("summary") or "") if cmp_result else "",
        }
        attached += 1
        if sid not in skus:
            skus.append(sid)

    return {"attached": attached, "skipped": skipped, "skus": skus}
