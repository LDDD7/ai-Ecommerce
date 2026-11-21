"""创意工单的编排层：读现状 → 改图 → 生成文案 → 合规检查 → 落库 → 可起草换素材动作。

三条设计取舍：

1. **改图能力来自 provider**。`local` 用 PIL 在现有合成图上重排（不联网、可离线、可复现）；
   `dashscope` 调百炼图生图（真出图，但要走网络、花额度）。provider 不可用时**自动降级**到 local，
   并在结果里写 `status="degraded"` 与原因，而不是直接报错——演示时没 key 也要能走通。
2. **合规是硬门**。文字占比超限、该有尺码表却没有、文案里有「最/第一/绝对」这类绝对化用语，
   一律进 `blockers`，此时 `apply()` 会拒绝起草动作。宁可这单不给用，也不能让不合规素材进审批。
3. **工单不直接生效**。`apply()` 只起草一条 `swap_creative` 动作（risk 1，需人审），
   走既有的审批 → 执行 → 回滚链路；这样「生成了什么」与「线上换没换」在审计里是两条独立记录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from app.creative import store as creative_store
from app.creative.schema import (
    AUTO_FIXABLE,
    MANUAL_ONLY,
    Compliance,
    ComplianceItem,
    CreativeAsset,
    CreativeConstraints,
    CreativeCopy,
    CreativeError,
    CreativeFix,
    CreativeRequest,
    CreativeResult,
)
from app.vision.schema import ISSUE_SPECS, derive_flags

# 文案里的绝对化用语：平台判定「虚假宣传」的高频词，宁可误杀。
BANNED_CLAIMS = ("最", "第一", "顶级", "绝对", "国家级", "全网最低", "永久")
SIZE_GUIDE = "S 胸围84 / M 胸围88 / L 胸围92 / XL 胸围96"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 读取现状


def _sku_row(db_path: str, sku_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sku_id, title, style, fabric, color, price_tag FROM dim_sku WHERE sku_id = ?",
            (sku_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise CreativeError(f"SKU 不存在：{sku_id}")
    return dict(zip(("sku_id", "title", "style", "fabric", "color", "price_tag"), row))


def _features(db_path: str, sku_id: str, role: str = "main") -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT path, features_json FROM image_feature WHERE sku_id = ? AND role = ? LIMIT 1",
            (sku_id, role),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise CreativeError(f"找不到主图：{sku_id}（role={role}）")
    path, raw = row
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
    data["_path"] = path
    return data


def _image_abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# ---------------------------------------------------------------- 文案与合规


def build_copy(sku: dict[str, Any], issues: list[str], constraints: CreativeConstraints) -> CreativeCopy:
    """文案跟着图改：图上文字变少，卖点就该进标题与详情，而不是挤在首图里。"""
    title = str(sku.get("title") or sku["sku_id"])
    if len(title) > 24:
        title = title[:24]
    fabric = sku.get("fabric") or ""
    color = sku.get("color") or ""
    style = sku.get("style") or ""
    price = sku.get("price_tag")

    points: list[str] = []
    if fabric and color:
        points.append(f"{fabric}面料 · {color}色，{style}日常可穿")
    if "TEXT_HEAVY" in issues:
        points.append("首图去文字，卖点移到详情页")
    if "PLAIN_WHITE" in issues:
        points.append("增加穿着场景，信息流里更好认")
    if "NO_SIZE_CHART" in issues and constraints.add_size_chart:
        points.append("补尺码表，减少尺码咨询与退货")
    if len(points) < 3:
        points.append("版型收腰，通勤与约会都能穿")
    points = points[:3]

    desc = f"{title}。{points[0]}。"
    if constraints.keep_price_tag and price:
        desc += f"当前售价 ¥{price:.0f}。"
    size_text = SIZE_GUIDE if (constraints.add_size_chart and "NO_SIZE_CHART" in issues) else ""
    return CreativeCopy(title=title, selling_points=points, description=desc, size_text=size_text)


def check_compliance(
    *,
    copy: CreativeCopy,
    after: dict[str, Any],
    issues: list[str],
    constraints: CreativeConstraints,
) -> Compliance:
    items: list[ComplianceItem] = []
    blockers: list[str] = []

    coverage = after.get("text_coverage")
    if isinstance(coverage, (int, float)):
        limit = constraints.max_text_coverage + 0.02
        if coverage <= limit:
            items.append(
                ComplianceItem(
                    code="text_coverage",
                    status="pass",
                    detail=f"文字占比 {coverage:.0%}，未超过 {limit:.0%}",
                )
            )
        else:
            msg = f"文字占比 {coverage:.0%} 仍超过上限 {constraints.max_text_coverage:.0%}"
            items.append(ComplianceItem(code="text_coverage", status="fail", detail=msg))
            blockers.append(msg)

    if constraints.add_size_chart and "NO_SIZE_CHART" in issues:
        ok = bool(after.get("size_chart_present"))
        items.append(
            ComplianceItem(
                code="size_chart",
                status="pass" if ok else "fail",
                detail="已补尺码表" if ok else "要求补尺码表，但改完仍没有",
            )
        )
        if not ok:
            blockers.append("缺尺码表")

    if "WATERMARK" in issues:
        clean = not bool(after.get("watermark"))
        items.append(
            ComplianceItem(
                code="watermark",
                status="pass" if clean else "fail",
                detail="水印已去除" if clean else "水印仍在",
            )
        )
        if not clean:
            blockers.append("水印未去除")

    hits = [word for word in BANNED_CLAIMS if word in copy.title or any(word in p for p in copy.selling_points)]
    if hits:
        msg = "文案含绝对化用语：" + "、".join(hits)
        items.append(ComplianceItem(code="claims", status="fail", detail=msg))
        blockers.append(msg)
    else:
        items.append(ComplianceItem(code="claims", status="pass", detail="未发现绝对化用语"))

    manual = [code for code in issues if code in MANUAL_ONLY]
    if manual:
        labels = "、".join(ISSUE_SPECS[code]["label"] for code in manual)
        items.append(
            ComplianceItem(code="manual_issues", status="warn", detail=f"{labels} 需要重拍或补拍，工单无法自动完成")
        )

    return Compliance(ok=not blockers, items=items, blockers=blockers)


# ---------------------------------------------------------------- 主流程


def _provider_chain(requested: str | None) -> tuple[str, str]:
    """返回 (最终 provider, 降级原因)。没 key 时把 dashscope 降成 local 而不是报错。"""
    from app.core import runtime

    want = requested or runtime.creative_provider() or "local"
    if want == "dashscope":
        try:
            from app.creative import dashscope

            if not dashscope.available():
                return "local", "未配置百炼 API Key，已降级为本地改图"
        except Exception as exc:  # 适配器本身出问题也不该挡住工单
            return "local", f"百炼适配器不可用（{type(exc).__name__}），已降级为本地改图"
    return want, ""


def _prompt(sku: dict[str, Any], issues: list[str], constraints: CreativeConstraints) -> str:
    labels = "、".join(ISSUE_SPECS[code]["label"] for code in issues if code in ISSUE_SPECS) or "提升主图质感"
    return (
        f"电商连衣裙主图，{sku.get('style') or ''}{sku.get('color') or ''}{sku.get('fabric') or ''}，"
        f"需要修复：{labels}。保持商品本体与颜色不变，去除图上营销文字，"
        f"文字面积不超过 {constraints.max_text_coverage:.0%}，背景为{constraints.scene}场景，"
        "人物自然站立，画面干净，方形构图。"
    )


def draft(
    db_path: str,
    request: CreativeRequest,
    *,
    actor: str = "agent",
) -> CreativeResult:
    started = time.perf_counter()
    sku = _sku_row(db_path, request.sku_id)
    feats = _features(db_path, request.sku_id)

    # 没传 issues 时按图的真实属性现推（演示时也可以只点一下按钮）
    issues = list(request.issues)
    if not issues:
        issues = derive_flags(
            text_coverage=float(feats.get("text_coverage") or 0.0),
            model_present=bool(feats.get("model_present")),
            scene=str(feats.get("scene") or ""),
            background=str(feats.get("background") or ""),
            size_chart_present=bool(feats.get("size_chart_present")),
            watermark=bool(feats.get("watermark")),
            collage=bool(feats.get("collage")),
        )

    provider, degrade_reason = _provider_chain(request.provider)
    draft_id = f"cr_{uuid.uuid4().hex[:12]}"
    before = {
        "image_url": f"/api/images/{request.sku_id}?role=main",
        "text_coverage": float(feats.get("text_coverage") or 0.0),
        "issues": issues,
        "path": feats.get("_path", ""),
    }

    after: dict[str, Any] = {}
    fixes: list[CreativeFix] = []
    assets: list[CreativeAsset] = []
    note = degrade_reason

    if provider in ("local", "mock"):
        from app.creative import local

        try:
            asset, after, fixes = local.render(
                db_path,
                sku_id=request.sku_id,
                issues=issues,
                constraints=request.constraints,
                draft_id=draft_id,
                target=request.target,
            )
        except ImportError as exc:
            raise CreativeError(f"本地改图器未就绪：{exc}") from exc
        assets = [asset]
    else:  # dashscope
        from app.creative import dashscope

        out_dir = PROJECT_ROOT / "data" / "creative" / request.sku_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{draft_id}_main.png"
        try:
            info = dashscope.edit_image(
                prompt=_prompt(sku, issues, request.constraints),
                base_image_path=str(_image_abs(str(feats.get("_path") or ""))),
                out_path=str(out_path),
                model=None,
            )
        except Exception as exc:
            # 真出图失败 → 降级本地，不让整张工单失败
            provider = "local"
            note = f"百炼生图失败（{type(exc).__name__}: {exc}），已降级为本地改图"
            from app.creative import local

            asset, after, fixes = local.render(
                db_path,
                sku_id=request.sku_id,
                issues=issues,
                constraints=request.constraints,
                draft_id=draft_id,
                target=request.target,
            )
            assets = [asset]
        else:
            rel = out_path.relative_to(PROJECT_ROOT).as_posix()
            assets = [
                CreativeAsset(
                    asset_id=f"{draft_id}-image",
                    path=rel,
                    url=f"/api/creative/asset/{draft_id}",
                    width=0,
                    height=0,
                )
            ]
            after = {"image_url": f"/api/creative/asset/{draft_id}", "issues_left": []}
            fixes = [
                CreativeFix(issue=code, action="regenerate", detail="由百炼图生图重绘", status="done")
                for code in issues
            ]
            note = note or f"由 {info.get('model') or '百炼'} 生成"

    copy = build_copy(sku, issues, request.constraints)
    compliance = check_compliance(copy=copy, after=after, issues=issues, constraints=request.constraints)

    manual = [code for code in issues if code in MANUAL_ONLY]
    left = list(after.get("issues_left") or [])
    left = [code for code in left if code not in manual]
    status = "degraded" if (degrade_reason or provider != (request.provider or provider)) else "ready"
    if after.get("text_coverage") is None:
        status = "degraded"
        note = note or "未拿到改后属性，无法核对文字占比"

    result = CreativeResult(
        draft_id=draft_id,
        sku_id=request.sku_id,
        provider=provider,
        status=status,
        target=request.target,
        before=before,
        after={
            **{k: v for k, v in after.items() if k != "image_url"},
            "image_url": after.get("image_url") or (assets[0].url if assets else ""),
            "issues_left": left,
        },
        assets=assets,
        copywriting=copy,
        compliance=compliance,
        fixes=fixes,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        note=note,
    )
    creative_store.save(db_path, result, request=request.model_dump(mode="json"))
    return result


def apply(
    db_path: str,
    draft_id: str,
    *,
    actor: str = "agent",
    reason: str = "",
) -> dict[str, Any]:
    """把工单起草成一条 `swap_creative` 动作（risk 1，仍需人审）。"""
    result = creative_store.get(db_path, draft_id)
    if not result.compliance.ok:
        raise CreativeError("合规检查未通过，不能起草动作：" + "；".join(result.compliance.blockers))
    image_path = (result.assets[0].path if result.assets else "") or ""

    from app.actions import store as action_store

    action = action_store.create_action(
        db_path,
        type="swap_creative",
        sku_id=result.sku_id,
        params={
            "creative_id": draft_id,
            "image_path": image_path,
            "reason": reason or f"创意工单 {draft_id}：{'/'.join(f.issue for f in result.fixes) or '素材优化'}",
        },
        trace_id=f"creative_{draft_id}",
        reason=reason or "创意工单起草",
        actor=actor,
    )
    return {"draft_id": draft_id, "action": action}
