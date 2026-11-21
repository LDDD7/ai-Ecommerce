"""素材检查入口：把一张商品图读成 :class:`MaterialFeatures`（mock / vlm / cache 三条路）。

设计要点
- **离线优先**：``mock`` 直接回放 ``image_feature.features_json`` 里的合成真值，
  中文标签经 :data:`CN_TO_CODE` 映射后，一律用 :func:`derive_flags` 按 schema 阈值重算，
  保证离线真值与模型现算走同一套判据。
- **模型字段不可信**：``vlm`` 只采信模型对图片的观察（文字、场景、水印等属性），
  问题码永远由 :func:`derive_flags` 重算，模型返回的 ``quality_flags`` 直接丢弃。
- **绝不把异常抛给上层**：除「SKU 不存在」抛 :class:`LookupError` 外，vlm 链路上的任何
  失败（无 key、超时、JSON 解析失败、schema 不符、图片读不出来）都回落到 mock 真值，
  并在 meta 里标记 ``degraded=True`` 与人话原因。
- **缓存键 = 图片内容 + 提示词版本 + 模型名**：``vision_cache`` 命中即可跳过模型调用，
  图片相同但提示词或模型换了不会误用旧结果。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.llm import LLMError, get_client
from app.vision.schema import (
    CN_TO_CODE,
    PROMPT_VERSION,
    MaterialFeatures,
    derive_flags,
    normalize_flags,
)

logger = logging.getLogger(__name__)

# 项目根目录（project/）：image_feature.path 是相对这里写的，例如 data/images/SKU0001_main.png。
ROOT = Path(__file__).resolve().parents[2]

# 合成数据里的中文标签 -> 问题码的翻译表放在 schema（单一来源），这里 re-export，
# 方便调用方 `from app.vision.inspector import CN_TO_CODE`。

# mode 入参允许的取值；auto 会在运行时按是否有真 key 折算成 mock/vlm。
_MODES = ("mock", "vlm", "auto")

# 提示词：只要求模型输出 JSON，键与 MaterialFeatures 的观察字段一一对应。
VISION_PROMPT = """你是电商素材质检员。请仔细看这张商品图，只输出一个 JSON 对象，不要解释、不要 markdown 代码块。
JSON 必须且只能包含以下 9 个键：
- text_blocks: 图上出现的文字内容，字符串数组；没有就给 []
- text_coverage: 文字（含白色文字底框、价签、促销标签）覆盖的面积占整张图的比例，取值 0~1 的小数
- model_present: 图中是否有真人、人台或假人模特在展示服装/商品；有任一即 true
- scene: 拍摄场景，只能取以下之一：「纯白底」「室内棚拍」「户外街拍」「度假场景」「居家」
- background: 背景类型，用中文短语，例如「纯色」「纯白」「街景」「室内」
- color_palette: 主色数组，最多 6 个十六进制颜色（如 "#F2E9DD"）
- size_chart_present: 图上是否有尺码表（含尺码/胸围/身高对照的表格或文字块），布尔值
- watermark: 图上是否有半透明水印字样，布尔值
- collage: 是否为多格拼接图（画面被分缝切成两块以上），布尔值
判读要点：文字面积按「肉眼看到的占比」估，不要按字数估；拼接图看有没有明显分缝；水印看有没有重复的半透明英文或 logo。
示例：{"text_blocks": ["限时 ¥148"], "text_coverage": 0.08, "model_present": true, "scene": "室内棚拍", "background": "纯色", "color_palette": ["#F2E9DD"], "size_chart_present": false, "watermark": false, "collage": false}
"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=5.0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_requested(mode: str | None) -> str:
    """把入参/配置折算成 mock 或 vlm。

    ``None`` 时读 ``settings.vision_mode``（字段不存在则退回 ``auto``）；
    ``auto`` 按 ``llm_active`` 判断：有真 key 走 vlm，否则 mock。
    """
    if mode is None:
        mode = getattr(get_settings(), "vision_mode", "auto")
    if mode not in _MODES:
        raise ValueError(f"未知的素材检查模式：{mode}")
    if mode == "auto":
        from app.core import runtime

        return "vlm" if runtime.llm_active() == "real" else "mock"
    return mode


def _image_path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _fetch_row(db_path: Path, sku_id: str, role: str) -> sqlite3.Row:
    """取该 SKU/角色的素材行；查不到抛 LookupError（这是唯一允许冒泡的异常）。"""
    if not db_path.exists():
        raise LookupError(f"没有找到 {sku_id} 的 {role} 素材")
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT image_id, sku_id, role, path, features_json, source "
            "FROM image_feature WHERE sku_id = ? AND role = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (sku_id, role),
        ).fetchone()
    if row is None:
        raise LookupError(f"没有找到 {sku_id} 的 {role} 素材")
    return row


def _features_from_payload(payload: dict[str, Any], sku_id: str, role: str, source: str) -> MaterialFeatures:
    """用观察字段构造 MaterialFeatures，问题码一律按阈值重算。

    只保留 schema 里认识的键，避免脏数据（例如多出来的调试字段）把整个检查拖垮；
    身份字段（sku_id/role/source/prompt_version）由调用方强制覆盖。
    """
    allowed = set(MaterialFeatures.model_fields)
    data = {k: v for k, v in payload.items() if k in allowed}
    data["sku_id"] = sku_id
    data["role"] = role
    data["source"] = source
    data["prompt_version"] = PROMPT_VERSION
    data["quality_flags"] = derive_flags(
        text_coverage=float(data.get("text_coverage", 0.0) or 0.0),
        model_present=bool(data.get("model_present", False)),
        scene=str(data.get("scene", "") or ""),
        background=str(data.get("background", "") or ""),
        size_chart_present=bool(data.get("size_chart_present", False)),
        watermark=bool(data.get("watermark", False)),
        collage=bool(data.get("collage", False)),
    )
    return MaterialFeatures(**data)


def _mock_features(row: sqlite3.Row, sku_id: str, role: str) -> MaterialFeatures:
    """回放离线真值；中文标签先映射成问题码，再由 derive_flags 重算最终 flags。"""
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(row["features_json"])
        if isinstance(parsed, dict):
            payload = parsed
    except (json.JSONDecodeError, TypeError):
        logger.warning("image_feature.features_json 无法解析，按空属性处理：%s", row["image_id"])

    raw_flags = payload.get("quality_flags")
    raw_flags = raw_flags if isinstance(raw_flags, list) else []
    mapped = normalize_flags(raw_flags)
    try:
        features = _features_from_payload(payload, sku_id, role, "mock")
    except Exception as exc:  # noqa: BLE001 - 真值脏了也不能把检查打断，退回空属性
        logger.warning("image_feature 真值不符合 schema，按空属性处理（%s）：%s", row["image_id"], exc)
        features = _features_from_payload({}, sku_id, role, "mock")
    if mapped and set(mapped) != set(features.quality_flags):
        # 真值标签与阈值重算不一致时不改写结果，只留痕：判据只有 derive_flags 一份。
        logger.debug(
            "真值标签 %s 与 derive_flags %s 不一致（%s），已按阈值重算",
            mapped,
            features.quality_flags,
            row["image_id"],
        )
    return features


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def ensure_cache_table(db_path: str | Path) -> None:
    """确保 ``vision_cache`` 表存在（幂等）。"""
    with closing(_connect(Path(db_path))) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vision_cache (
                image_sha256   TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                model          TEXT NOT NULL,
                features_json  TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                PRIMARY KEY (image_sha256, prompt_version, model)
            )
            """
        )
        conn.commit()


def _cache_get(db_path: Path, image_sha256: str, model: str) -> MaterialFeatures | None:
    """读缓存；表不存在或内容损坏都当未命中，绝不影响主流程。"""
    try:
        with closing(_connect(db_path)) as conn:
            row = conn.execute(
                "SELECT features_json FROM vision_cache "
                "WHERE image_sha256 = ? AND prompt_version = ? AND model = ?",
                (image_sha256, PROMPT_VERSION, model),
            ).fetchone()
        if row and isinstance(row[0], str):
            data = json.loads(row[0])
            if isinstance(data, dict):
                return MaterialFeatures(**data)
    except Exception as exc:  # noqa: BLE001 - 缓存故障必须静默
        logger.debug("读取素材缓存失败（已忽略）：%s", exc)
    return None


def _cache_put(db_path: Path, image_sha256: str, model: str, features: MaterialFeatures) -> None:
    """写缓存；失败只记日志，不影响本次结果。"""
    try:
        with closing(_connect(db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vision_cache "
                "(image_sha256, prompt_version, model, features_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    image_sha256,
                    PROMPT_VERSION,
                    model,
                    json.dumps(features.model_dump(), ensure_ascii=False),
                    _now(),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 缓存故障必须静默
        logger.debug("写入素材缓存失败（已忽略）：%s", exc)


# ---------------------------------------------------------------------------
# vlm 路径
# ---------------------------------------------------------------------------


def _vlm_features(
    db_path: Path,
    row: sqlite3.Row,
    sku_id: str,
    role: str,
    llm: Any | None,
) -> tuple[MaterialFeatures | None, bool, str]:
    """尝试走模型；成功返回 (features, False, "")，失败返回 (None, degraded, reason)。"""
    img_path = _image_path(row["path"])
    if not img_path.is_file():
        return None, True, f"素材图片文件不存在：{img_path}，已回落到离线真值"

    try:
        image_bytes = img_path.read_bytes()
    except OSError as exc:
        return None, True, f"素材图片文件读取失败：{exc}，已回落到离线真值"

    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    from app.core import runtime

    model = runtime.model_for("vision")

    # 缓存只对成功过的 vlm 结果有意义；建表失败也不该挡住本次检查。
    try:
        ensure_cache_table(db_path)
        cached = _cache_get(db_path, image_sha256, model)
    except Exception as exc:  # noqa: BLE001
        logger.debug("素材缓存不可用（已忽略）：%s", exc)
        cached = None
    if cached is not None:
        # 缓存键不含 SKU/角色（同一张图可能被多个 SKU 复用），命中后必须覆盖身份字段。
        return (
            cached.model_copy(
                update={"sku_id": sku_id, "role": role, "source": "cache", "prompt_version": PROMPT_VERSION}
            ),
            False,
            "",
        )

    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": "你是电商素材质检员，只输出 JSON。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
            ],
        },
    ]

    client = llm if llm is not None else get_client()
    try:
        data = client.chat_json(messages, model=model, temperature=0.0, max_tokens=500)
    except LLMError as exc:
        return None, True, f"模型调用失败（{exc}），已回落到离线真值"
    except Exception as exc:  # noqa: BLE001 - 假客户端/网关都可能抛任意异常
        return None, True, f"模型调用失败（{type(exc).__name__}: {exc}），已回落到离线真值"

    if not isinstance(data, dict):
        return None, True, "模型返回不符合 schema，已回落到离线真值"

    try:
        # 身份字段与问题码先按本地口径写死再校验：模型说什么都不采信，
        # 但未知的多余键仍会让 pydantic 报错，从而触发降级（垃圾 JSON 进不来）。
        payload = dict(data)
        payload["sku_id"] = sku_id
        payload["role"] = role
        payload["source"] = "vlm"
        payload["prompt_version"] = PROMPT_VERSION
        payload["quality_flags"] = []
        features = MaterialFeatures(**payload)
        features = features.model_copy(
            update={
                "quality_flags": derive_flags(
                    text_coverage=features.text_coverage,
                    model_present=features.model_present,
                    scene=features.scene,
                    background=features.background,
                    size_chart_present=features.size_chart_present,
                    watermark=features.watermark,
                    collage=features.collage,
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001 - pydantic 校验失败也走降级
        logger.debug("模型返回不符合 MaterialFeatures：%s", exc)
        return None, True, "模型返回不符合 schema，已回落到离线真值"

    _cache_put(db_path, image_sha256, model, features)
    return features, False, ""


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def inspect(
    db_path: str | Path,
    sku_id: str,
    role: str = "main",
    *,
    mode: str | None = None,
    llm=None,
) -> tuple[MaterialFeatures, dict]:
    """检查单个 SKU 的素材属性。

    返回 ``(features, meta)``。除 SKU 不存在抛 :class:`LookupError` 外，任何 vlm 失败都会
    回落到 mock 真值并在 meta 里给出 ``degraded=True`` 与 ``reason``。
    """
    started = time.perf_counter()
    db = Path(db_path)
    requested = _resolve_requested(mode)
    row = _fetch_row(db, sku_id, role)

    degraded = False
    cached = False
    reason = ""

    if requested == "vlm":
        features, degraded, reason = _vlm_features(db, row, sku_id, role, llm)
        if features is not None:
            cached = features.source == "cache"
        else:
            features = _mock_features(row, sku_id, role)
    else:
        features = _mock_features(row, sku_id, role)

    # 实际生效模式：请求 mock 或发生降级都是 mock，否则是 vlm（含缓存命中）。
    actual_mode = "mock" if (requested == "mock" or degraded) else "vlm"
    meta = {
        "requested": requested,
        "mode": actual_mode,
        "degraded": degraded,
        "reason": reason,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "cached": cached,
        "image_id": row["image_id"],
        "prompt_version": features.prompt_version,
        "source": features.source,
    }
    return features, meta


def inspect_many(
    db_path: str | Path,
    sku_ids: list[str],
    role: str = "main",
    *,
    mode: str | None = None,
) -> tuple[list[MaterialFeatures], dict]:
    """批量检查；逐个走 :func:`inspect`，meta 汇总数量/降级/缓存/耗时。"""
    started = time.perf_counter()
    requested = _resolve_requested(mode)

    features_list: list[MaterialFeatures] = []
    items: list[dict[str, Any]] = []
    item_metas: list[dict] = []
    for sku_id in sku_ids:
        features, meta = inspect(db_path, sku_id, role, mode=mode)
        features_list.append(features)
        item_metas.append(meta)
        items.append(
            {
                "sku_id": sku_id,
                "source": features.source,
                "degraded": bool(meta["degraded"]),
            }
        )

    modes = {meta["mode"] for meta in item_metas}
    if not modes:
        summary_mode = requested
    elif len(modes) == 1:
        summary_mode = next(iter(modes))
    else:
        # 有的走通、有的降级：如实标成 mixed，不掩盖部分失败。
        summary_mode = "mixed"
    degraded = any(item["degraded"] for item in items)
    meta = {
        "count": len(features_list),
        "degraded": degraded,
        "cached": all(item["source"] == "cache" for item in items) if items else False,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "requested": requested,
        "mode": summary_mode,
        "items": items,
    }
    return features_list, meta
