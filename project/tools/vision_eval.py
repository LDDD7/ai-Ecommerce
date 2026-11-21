"""素材诊断评测（spec §19.5 的 vision suite）。

10 条用例都来自合成图的真值（`image_feature.features_json`）：合成图是我们画的，
每个属性天然知道正确答案，所以这里能算准确率，而不是「看着像对」。

判分口径（与 §19.5 一致）：
- `size_chart_present` / `watermark` / `collage` / `model_present`：完全一致才算对；
- `text_coverage`：允许 ±0.10 的误差（模型只能估面积，不可能数像素）；
- `scene` / `background`：按「是否同义」判——都归一到「室内/户外/纯白底」三档再比。

用法：
    python -m tools.vision_eval                 # 默认 auto（有 key 现算，无 key 回放真值）
    python -m tools.vision_eval --mode vlm      # 强制模型现算，跑真链路
    python -m tools.vision_eval --mode mock     # 只验评测脚本本身（应得 1.0）
    python -m tools.vision_eval --cases 20 --min-accuracy 0.85
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.vision.schema import CN_TO_CODE, MaterialFeatures  # noqa: E402

BOOL_FIELDS = ("size_chart_present", "watermark", "collage", "model_present")
COVERAGE_TOLERANCE = 0.10


def _scene_bucket(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "未知"
    if "白" in text and ("底" in text or "背景" in text):
        return "纯白底"
    if any(k in text for k in ("度假", "海边", "沙滩")):
        return "度假"
    if any(k in text for k in ("街", "户外", "外景", "公园", "城市")):
        return "户外"
    if any(k in text for k in ("室内", "棚", "居家", "家")):
        return "室内"
    return text


def load_ground_truth(db_path: str | Path, cases: int = 10, seed: int = 7) -> list[dict[str, Any]]:
    """按问题码分层抽样：保证 10 条里既有「无尺码表」，也有「有水印/拼接」的。"""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT sku_id, role, features_json FROM image_feature ORDER BY sku_id").fetchall()
    finally:
        con.close()

    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = json.loads(row["features_json"])
        flags = [CN_TO_CODE.get(f, f) for f in data.get("quality_flags") or []]
        data["quality_flags"] = flags
        data["sku_id"] = row["sku_id"]
        data["role"] = row["role"]
        for flag in flags or ["CLEAN"]:
            buckets.setdefault(flag, []).append(data)

    rng = random.Random(seed)
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for flag in sorted(buckets, key=lambda f: (f == "CLEAN", f)):
        pool = [d for d in buckets[flag] if d["sku_id"] not in seen]
        if not pool:
            continue
        item = rng.choice(pool)
        picked.append(item)
        seen.add(item["sku_id"])
        if len(picked) >= cases:
            break
    while len(picked) < cases and rows:
        item = next((json.loads(r["features_json"]) for r in rows if r["sku_id"] not in seen), None)
        if item is None:
            break
        item["sku_id"] = next(r["sku_id"] for r in rows if r["sku_id"] not in seen)
        item["role"] = "main"
        item["quality_flags"] = [CN_TO_CODE.get(f, f) for f in item.get("quality_flags") or []]
        picked.append(item)
        seen.add(item["sku_id"])
    return picked[:cases]


FIELDS = BOOL_FIELDS + ("text_coverage", "scene")


def score_case(gt: dict[str, Any], got: MaterialFeatures) -> dict[str, Any]:
    """逐字段判分；返回本条的对错、字段级对错与错误明细。

    两个口径：
    - `ok`（整条正确）：6 个字段全对才算一条对，最严；
    - `fields_ok/total_fields`（字段级）：素材抽取是「多标签」任务，按字段算准确率更贴近
      spec §12.1 的「素材属性抽取准确率 ≥0.85」，也更能反映模型到底哪一项不行。
    """
    wrong: list[str] = []
    field_ok: dict[str, bool] = {}
    for field in BOOL_FIELDS:
        correct = bool(gt.get(field)) == bool(getattr(got, field))
        field_ok[field] = correct
        if not correct:
            wrong.append(f"{field}: 期望 {gt.get(field)} 得到 {getattr(got, field)}")
    gt_cov = float(gt.get("text_coverage") or 0.0)
    cov_ok = abs(gt_cov - got.text_coverage) <= COVERAGE_TOLERANCE
    field_ok["text_coverage"] = cov_ok
    if not cov_ok:
        wrong.append(f"text_coverage: 期望 {gt_cov:.2f} 得到 {got.text_coverage:.2f}")
    scene_ok = _scene_bucket(str(gt.get("scene") or "")) == _scene_bucket(got.scene)
    field_ok["scene"] = scene_ok
    if not scene_ok:
        wrong.append(f"scene: 期望 {gt.get('scene')} 得到 {got.scene}")
    return {
        "sku_id": gt["sku_id"],
        "ok": not wrong,
        "fields_ok": sum(1 for v in field_ok.values() if v),
        "total_fields": len(field_ok),
        "field_detail": field_ok,
        "errors": wrong,
        "source": got.source,
        "coverage_gt": round(gt_cov, 3),
        "coverage_got": round(got.text_coverage, 3),
        "flags_gt": gt.get("quality_flags") or [],
        "flags_got": list(got.quality_flags),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="素材诊断评测（10 条 vision suite）")
    parser.add_argument("--mode", default=None, choices=["auto", "mock", "vlm"], help="检查模式，默认按配置")
    parser.add_argument("--cases", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--min-accuracy", type=float, default=0.85)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    args = parser.parse_args(argv)

    settings = get_settings()
    from app.vision.inspector import inspect

    cases = load_ground_truth(settings.db_path, cases=args.cases, seed=args.seed)
    results = []
    t0 = time.perf_counter()
    for gt in cases:
        features, meta = inspect(settings.db_path, gt["sku_id"], "main", mode=args.mode)
        row = score_case(gt, features)
        row["degraded"] = bool(meta.get("degraded"))
        row["mode"] = meta.get("mode")
        results.append(row)
    elapsed = (time.perf_counter() - t0) * 1000

    passed = sum(1 for r in results if r["ok"])
    accuracy = passed / len(results) if results else 0.0
    fields_ok = sum(r["fields_ok"] for r in results)
    total_fields = sum(r["total_fields"] for r in results)
    field_accuracy = fields_ok / total_fields if total_fields else 0.0
    payload = {
        "suite": "vision",
        "mode": args.mode or settings.vision_mode,
        "cases": len(results),
        "passed": passed,
        "accuracy": round(accuracy, 4),
        "field_accuracy": round(field_accuracy, 4),
        "fields_ok": fields_ok,
        "total_fields": total_fields,
        "min_accuracy": args.min_accuracy,
        "degraded": sum(1 for r in results if r["degraded"]),
        "elapsed_ms": round(elapsed, 1),
        "results": results,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"素材诊断评测：{len(results)} 条，整条通过 {passed}（{accuracy:.2%}），"
              f"字段准确率 {field_accuracy:.2%}（{fields_ok}/{total_fields}），"
              f"降级 {payload['degraded']} 条，耗时 {elapsed:.0f}ms（mode={payload['mode']}）")
        for r in results:
            mark = "✓" if r["ok"] else "✗"
            extra = "" if r["ok"] else "  " + "；".join(r["errors"][:3])
            print(f"  {mark} {r['sku_id']}  {r['fields_ok']}/{r['total_fields']}  {r['source']}{extra}")

    ok = field_accuracy >= args.min_accuracy
    if not args.json:
        print("门禁：" + ("通过" if ok else f"未通过（字段准确率 < {args.min_accuracy:.0%}）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
