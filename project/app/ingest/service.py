"""导入流程的两个阶段：预览（不动库）与提交（进库）。

预览把识别结果、质量报告和一份「草稿」落到 `.tmp/ingest/previews/`，
提交只认 token，商家在界面上确认过的映射不会在两次请求之间丢失。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from app.ingest import quality, store
from app.ingest.canonical import DATASETS, canonical_payload
from app.ingest.detect import apply_mapping, detect
from app.ingest.loaders import Table, read_table

MAX_BYTES = 50 * 1024 * 1024


class PreviewError(ValueError):
    """预览失败：文件读不了、格式不支持、映射不完整。"""


def _root_dir(root: Path) -> Path:
    path = root / ".tmp" / "ingest" / "previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_preview(
    data: bytes,
    filename: str,
    *,
    dataset: str | None = None,
    mapping: dict[str, str] | None = None,
    root: Path,
) -> dict[str, Any]:
    if not data:
        raise PreviewError("文件是空的")
    if len(data) > MAX_BYTES:
        raise PreviewError("文件超过 50MB，请先按时间范围拆成几份")
    try:
        table = read_table(data, filename)
    except ValueError as exc:
        raise PreviewError(str(exc)) from exc

    detection = detect(table, dataset) if dataset is None else None
    target = dataset or (detection.dataset if detection else "")
    if target not in DATASETS:
        raise PreviewError("认不出这是订单、流量、商品还是退款表，请手动指定")

    final_mapping = mapping or (detection.mapping if detection else {})
    if not final_mapping:
        raise PreviewError("没有可用的字段映射")

    # 必需字段对不上就别让人白点「确认入库」：一次说清楚缺哪几列。
    missing = [
        field.label
        for field in DATASETS[target].fields
        if field.required and field.name not in final_mapping
    ]
    if missing:
        raise PreviewError("缺少必需字段：" + "、".join(missing))

    report = quality.check(target, table, final_mapping)
    token = secrets.token_hex(8)
    digest = _sha(data)
    suffix = Path(filename).suffix.lower() or ".csv"
    upload = _root_dir(root) / f"{token}{suffix}"
    upload.write_bytes(data)

    payload = {
        "token": token,
        "dataset": target,
        "filename": filename,
        "file_sha256": digest,
        "mapping": final_mapping,
        "headers": list(table.headers),
        "report": report.as_dict(),
        "upload": str(upload),
    }
    (_root_dir(root) / f"{token}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "token": token,
        "dataset": target,
        "dataset_label": DATASETS[target].label,
        "filename": filename,
        "headers": list(table.headers),
        "header_row": table.header_row,
        "row_count": len(table.rows),
        "mapping": final_mapping,
        "detection": detection.as_dict() if detection else None,
        "report": report.as_dict(),
        "preview_rows": table.dict_rows()[:5],
    }


def load_preview(token: str, *, root: Path) -> dict[str, Any]:
    path = _root_dir(root) / f"{token}.json"
    if not path.exists():
        raise PreviewError("预览已过期，请重新上传文件")
    return json.loads(path.read_text(encoding="utf-8"))


def commit_preview(
    token: str,
    *,
    db_path: str,
    root: Path,
    allow_warn: bool = True,
    confirm_changes: bool = False,
) -> dict[str, Any]:
    payload = load_preview(token, root=root)
    verdict = str(payload["report"].get("verdict") or "")
    if verdict == "reject":
        raise ValueError("数据质量不达标（" + "；".join(payload["report"].get("reasons") or []) + "）")
    if verdict == "warn" and not allow_warn:
        raise ValueError("质量报告有警告，需要确认后才能入库")

    upload = Path(payload["upload"])
    if not upload.exists():
        raise PreviewError("源文件已丢失，请重新上传")
    table: Table = read_table(upload.read_bytes(), payload["filename"])
    dataset = payload["dataset"]
    mapping = payload["mapping"]
    rows, errors = apply_mapping(table, dataset, mapping)
    if errors and len(errors) > max(5, len(rows) * 0.1):
        raise ValueError("超过 10% 的行无法解析，已中止入库")

    result = store.commit(
        db_path,
        dataset,
        rows,
        filename=payload["filename"],
        file_sha256=payload["file_sha256"],
        report=payload["report"],
        mapping=mapping,
        root=root,
        confirm_changes=confirm_changes,
    )
    result["dataset_label"] = DATASETS[dataset].label
    result["skipped"] = len(errors)
    result["capabilities"] = quality.capabilities(db_path)
    return result


def undo_run(run_id: str, *, db_path: str) -> dict[str, Any]:
    result = store.undo_run(db_path, run_id)
    result["capabilities"] = quality.capabilities(db_path)
    return result


def schema_payload() -> dict[str, Any]:
    return canonical_payload()
