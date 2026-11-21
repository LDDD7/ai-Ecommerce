"""素材与「同款头部商品」的中位基准对比（peer-v1）。

先说清口径：本项目**没有真实竞品图**。`peer_benchmark` 取的是同 `style`
近 30 天销量 Top-N 商品的素材属性中位数/覆盖率，代表「同款式卖得好的商品
一般长什么样」，不是某一张具体竞品主图。因此文案里统一说「同款式中位」，
不把它写成「竞品」。

只读 `store.db`，不依赖视觉模型：属性来自 `image_feature.features_json`。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

PEER_LIMIT = 10

# 销量窗口：以 fact_orders 的最新一天为锚点，回看 30 天。
PEER_WINDOW_DAYS = 30

# 文字占比差距超过 10 个百分点才算「我们更差 / 更好」。
TEXT_GAP_WORSE = 0.10

# 布尔属性的「同行覆盖率」阈值：>=0.6 说明多数同行都有，缺了就是短板；
# <=0.3 说明同行很少这么干，我们有就是短板（水印/拼接）。
PRESENCE_PEER_RATE = 0.6
OUTLIER_PEER_RATE = 0.3

FEATURE_FIELDS = (
    "sku_id",
    "role",
    "text_blocks",
    "text_coverage",
    "model_present",
    "scene",
    "background",
    "color_palette",
    "size_chart_present",
    "watermark",
    "collage",
    "quality_flags",
    "confidence",
    "source",
)


def _default_features(sku_id: str, role: str) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "role": role,
        "text_blocks": [],
        "text_coverage": 0.0,
        "model_present": False,
        "scene": "",
        "background": "",
        "color_palette": [],
        "size_chart_present": False,
        "watermark": False,
        "collage": False,
        "quality_flags": [],
        "confidence": 1.0,
        "source": "missing",
    }


def _connect(db_path: str | Path) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.exists():
        return None
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def _parse_features(raw: str | None, *, sku_id: str, role: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out = _default_features(sku_id, role)
    out.update(data)
    out["sku_id"] = sku_id
    out["role"] = str(data.get("role") or role)
    if not out.get("source"):
        out["source"] = "mock"
    return out


def _load_own_features(
    con: sqlite3.Connection, sku_id: str, role: str
) -> dict[str, Any]:
    row = con.execute(
        "SELECT features_json, source FROM image_feature "
        "WHERE sku_id = ? AND role = ? ORDER BY created_at DESC LIMIT 1",
        (sku_id, role),
    ).fetchone()
    if not row:
        return _default_features(sku_id, role)
    feats = _parse_features(row[0], sku_id=sku_id, role=role)
    if feats is None:
        return _default_features(sku_id, role)
    feats["source"] = feats.get("source") or row[1] or "mock"
    return feats


def _rate(values: list[bool]) -> float:
    if not values:
        return 0.0
    return round(sum(1 for v in values if v) / len(values), 3)


def _empty_peer(style: str) -> dict[str, Any]:
    return {
        "style": style,
        "peer_count": 0,
        "text_coverage_median": 0.0,
        "size_chart_rate": 0.0,
        "model_present_rate": 0.0,
        "watermark_rate": 0.0,
        "collage_rate": 0.0,
        "scope": "同款式中位",
    }


def peer_benchmark(
    db_path: str | Path,
    sku_id: str,
    *,
    role: str = "main",
    limit: int = PEER_LIMIT,
) -> dict[str, Any]:
    """同款式销量 Top-`limit` 商品的素材中位/覆盖率。

    同行不足 3 个、甚至一个都没有时照常返回，`peer_count` 给实际值，
    不抛错——上层据此把对比降级为「样本不足」即可。
    """
    con = _connect(db_path)
    if con is None:
        return _empty_peer("")

    try:
        row = con.execute(
            "SELECT style FROM dim_sku WHERE sku_id = ?", (sku_id,)
        ).fetchone()
        style = str(row[0]) if row and row[0] else ""
        if not style:
            return _empty_peer("")
        if limit <= 0:
            return _empty_peer(style)

        max_dt = con.execute("SELECT MAX(dt) FROM fact_orders").fetchone()[0]
        if not max_dt:
            return _empty_peer(style)

        # 只把「确实有该 role 素材属性」的 SKU 纳入候选，避免 Top-N 被
        # 没有 image_feature 行的商品占掉名额。
        rows = con.execute(
            "SELECT s.sku_id, SUM(o.qty) AS qty "
            "FROM dim_sku s "
            "JOIN fact_orders o ON o.sku_id = s.sku_id "
            "WHERE s.style = ? AND s.sku_id <> ? "
            "  AND o.dt >= date(?, ?) "
            "  AND EXISTS (SELECT 1 FROM image_feature f "
            "              WHERE f.sku_id = s.sku_id AND f.role = ?) "
            "GROUP BY s.sku_id "
            "ORDER BY qty DESC, s.sku_id "
            "LIMIT ?",
            (style, sku_id, max_dt, f"-{PEER_WINDOW_DAYS} day", role, int(limit)),
        ).fetchall()
        peer_ids = [str(r[0]) for r in rows]
        if not peer_ids:
            return _empty_peer(style)

        marks = ",".join("?" for _ in peer_ids)
        feat_rows = con.execute(
            f"SELECT sku_id, features_json FROM image_feature "
            f"WHERE role = ? AND sku_id IN ({marks}) "
            f"ORDER BY created_at DESC",
            (role, *peer_ids),
        ).fetchall()
    finally:
        con.close()

    feats: dict[str, dict[str, Any]] = {}
    for pid, raw in feat_rows:
        sid = str(pid)
        if sid in feats:
            continue
        parsed = _parse_features(raw, sku_id=sid, role=role)
        if parsed is not None:
            feats[sid] = parsed

    if not feats:
        return _empty_peer(style)

    coverages = [float(f.get("text_coverage") or 0.0) for f in feats.values()]
    return {
        "style": style,
        "peer_count": len(feats),
        "text_coverage_median": round(statistics.median(coverages), 4),
        "size_chart_rate": _rate([bool(f.get("size_chart_present")) for f in feats.values()]),
        "model_present_rate": _rate([bool(f.get("model_present")) for f in feats.values()]),
        "watermark_rate": _rate([bool(f.get("watermark")) for f in feats.values()]),
        "collage_rate": _rate([bool(f.get("collage")) for f in feats.values()]),
        "scope": "同款式中位",
    }


def _text_verdict(ours: float, peer: float) -> str:
    """文字占比：高于同行 10 个百分点以上算 worse，低于 10 个点以上算 better。"""
    delta = float(ours) - float(peer)
    if delta > TEXT_GAP_WORSE:
        return "worse"
    if delta < -TEXT_GAP_WORSE:
        return "better"
    return "same"


def _text_gap(ours: float, peer: float) -> dict[str, Any]:
    verdict = _text_verdict(ours, peer)
    delta = round(float(ours) - float(peer), 4)
    if verdict == "worse":
        text = (
            f"文字占比 {ours:.0%}，比同款式中位 {peer:.0%} "
            f"高 {delta * 100:.0f} 个百分点"
        )
    elif verdict == "better":
        text = (
            f"文字占比 {ours:.0%}，比同款式中位 {peer:.0%} "
            f"低 {-delta * 100:.0f} 个百分点"
        )
    else:
        text = f"文字占比 {ours:.0%}，与同款式中位 {peer:.0%} 基本持平"
    return {
        "field": "text_coverage",
        "ours": round(float(ours), 4),
        "peer": round(float(peer), 4),
        "delta": delta,
        "verdict": verdict,
        "text": text,
    }


def _presence_gap(
    *,
    field: str,
    ours: bool,
    peer_rate: float,
    worse_text: str,
    better_text: str,
    same_text: str,
) -> dict[str, Any]:
    """尺码表/模特这类「同行普遍有，缺了就是短板」的布尔属性。"""
    if not ours and peer_rate >= PRESENCE_PEER_RATE:
        verdict = "worse"
        text = worse_text
    elif ours and peer_rate <= 1.0 - PRESENCE_PEER_RATE:
        verdict = "better"
        text = better_text
    else:
        verdict = "same"
        text = same_text
    return {
        "field": field,
        "ours": bool(ours),
        "peer": round(float(peer_rate), 3),
        "delta": round(float(ours) - float(peer_rate), 3),
        "verdict": verdict,
        "text": text,
    }


def _outlier_gap(
    *,
    field: str,
    ours: bool,
    peer_rate: float,
    worse_text: str,
    better_text: str,
    same_text: str,
) -> dict[str, Any]:
    """水印/拼接这类「同行很少这么干，我们有就是短板」的布尔属性。"""
    if ours and peer_rate <= OUTLIER_PEER_RATE:
        verdict = "worse"
        text = worse_text
    elif not ours and peer_rate >= PRESENCE_PEER_RATE:
        verdict = "better"
        text = better_text
    else:
        verdict = "same"
        text = same_text
    return {
        "field": field,
        "ours": bool(ours),
        "peer": round(float(peer_rate), 3),
        "delta": round(float(ours) - float(peer_rate), 3),
        "verdict": verdict,
        "text": text,
    }


def _build_gaps(ours: dict[str, Any], peer: dict[str, Any]) -> list[dict[str, Any]]:
    text = _text_gap(
        float(ours.get("text_coverage") or 0.0),
        float(peer.get("text_coverage_median") or 0.0),
    )

    size_rate = float(peer.get("size_chart_rate") or 0.0)
    size = _presence_gap(
        field="size_chart_present",
        ours=bool(ours.get("size_chart_present")),
        peer_rate=size_rate,
        worse_text=f"主图缺尺码表，同款式 {size_rate:.0%} 的头部商品有尺码表",
        better_text=f"主图有尺码表，同款式仅 {size_rate:.0%} 的头部商品有尺码表",
        same_text=(
            f"尺码表覆盖与同款式中位接近"
            f"（我方{'有' if ours.get('size_chart_present') else '无'}，"
            f"同行 {size_rate:.0%}）"
        ),
    )

    model_rate = float(peer.get("model_present_rate") or 0.0)
    model = _presence_gap(
        field="model_present",
        ours=bool(ours.get("model_present")),
        peer_rate=model_rate,
        worse_text=f"主图没有模特上身，同款式 {model_rate:.0%} 的头部商品有模特",
        better_text=f"主图有模特上身，同款式仅 {model_rate:.0%} 的头部商品有模特",
        same_text=(
            f"模特覆盖与同款式中位接近"
            f"（我方{'有' if ours.get('model_present') else '无'}，"
            f"同行 {model_rate:.0%}）"
        ),
    )

    wm_rate = float(peer.get("watermark_rate") or 0.0)
    watermark = _outlier_gap(
        field="watermark",
        ours=bool(ours.get("watermark")),
        peer_rate=wm_rate,
        worse_text=f"主图带水印，同款式仅 {wm_rate:.0%} 的头部商品带水印",
        better_text=f"主图无水印，同款式 {wm_rate:.0%} 的头部商品带水印",
        same_text=(
            f"水印情况与同款式中位接近"
            f"（我方{'有' if ours.get('watermark') else '无'}，"
            f"同行 {wm_rate:.0%}）"
        ),
    )

    col_rate = float(peer.get("collage_rate") or 0.0)
    collage = _outlier_gap(
        field="collage",
        ours=bool(ours.get("collage")),
        peer_rate=col_rate,
        worse_text=f"主图是拼接图，同款式仅 {col_rate:.0%} 的头部商品用拼接图",
        better_text=f"主图不是拼接图，同款式 {col_rate:.0%} 的头部商品用拼接图",
        same_text=(
            f"拼接情况与同款式中位接近"
            f"（我方{'是' if ours.get('collage') else '不是'}拼接图，"
            f"同行 {col_rate:.0%}）"
        ),
    )

    return [text, size, model, watermark, collage]


# 总结时优先讲更贴近成交的短板。
_SUMMARY_PRIORITY = {
    "size_chart_present": 0,
    "text_coverage": 1,
    "watermark": 2,
    "model_present": 3,
    "collage": 4,
}


def _summarize(gaps: list[dict[str, Any]]) -> str:
    worse = [g for g in gaps if g.get("verdict") == "worse"]
    if not worse:
        return "主图素材各项指标与同款式中位基本持平，暂无明显短板"
    worse.sort(key=lambda g: _SUMMARY_PRIORITY.get(str(g.get("field")), 99))
    top = worse[0]
    extra = f"；另有 {len(worse) - 1} 项弱于同款式中位" if len(worse) > 1 else ""
    return f"主图素材最明显的短板是{top['text']}{extra}"


def compare(
    db_path: str | Path,
    sku_id: str,
    *,
    role: str = "main",
    limit: int = PEER_LIMIT,
) -> dict[str, Any]:
    """我方主图属性 vs 同款式中位，输出可解释的 gaps 与一句总结。"""
    started = time.perf_counter()
    peer = peer_benchmark(db_path, sku_id, role=role, limit=limit)

    con = _connect(db_path)
    if con is None:
        ours = _default_features(sku_id, role)
    else:
        try:
            ours = _load_own_features(con, sku_id, role)
        finally:
            con.close()

    ours = {k: ours.get(k) for k in FEATURE_FIELDS}
    gaps = _build_gaps(ours, peer)
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    return {
        "sku_id": sku_id,
        "role": role,
        "ours": ours,
        "peer": peer,
        "gaps": gaps,
        "summary": _summarize(gaps),
        "meta": {"engine": "peer-v1", "elapsed_ms": elapsed_ms, "degraded": False},
    }
