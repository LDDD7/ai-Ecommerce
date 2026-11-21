"""``app.vision.inspector`` 的离线测试：不联网、不加载真模型、图片用内存里造的最小 PNG。

mock 路径读 conftest 的 20 SKU 测试库（``DB_PATH``）；vlm 路径一律注入只实现
``chat_json`` 的假客户端，避免任何网络与额度消耗。
"""

from __future__ import annotations

import base64
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.llm import LLMError
from app.vision.inspector import (
    CN_TO_CODE,
    ensure_cache_table,
    inspect,
    inspect_many,
)
from app.vision.schema import ISSUE_SPECS, PROMPT_VERSION, derive_flags

# 本机 %TEMP% 目录 ACL 异常，测试产物统一放工作区 .tmp 下（见 tests/conftest.py）。
_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "test_vision"

# 1x1 透明 PNG：inspector 只读字节，不解码，所以足够用来算 sha256 / 拼 data URL。
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# 夹具与假客户端
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir() -> Path:
    path = _TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def vision_db(workdir: Path) -> Path:
    """复制一份 session 级测试库，测试里可以随意改 path / 写缓存而不互相污染。"""
    src = Path(get_settings().db_path)
    dst = workdir / "store.db"
    with closing(sqlite3.connect(str(src))) as source, closing(sqlite3.connect(str(dst))) as target:
        source.backup(target)
    return dst


def _image_file(workdir: Path) -> Path:
    path = workdir / "main.png"
    path.write_bytes(_PNG_1X1)
    return path


def _set_path(db: Path, sku_id: str, role: str, path: Path | str) -> None:
    with closing(sqlite3.connect(str(db))) as con:
        con.execute(
            "UPDATE image_feature SET path = ? WHERE sku_id = ? AND role = ?",
            (str(path), sku_id, role),
        )
        con.commit()


def _valid_payload() -> dict:
    """一份合法的模型观察；``quality_flags`` 故意与阈值冲突，用来验证不被采信。"""
    return {
        "text_blocks": ["限时 ¥148", "通勤显瘦"],
        "text_coverage": 0.5,
        "model_present": True,
        "scene": "室内棚拍",
        "background": "纯色",
        "color_palette": ["#f2e9dd", "8b6f5c"],
        "size_chart_present": True,
        "watermark": False,
        "collage": False,
        "quality_flags": ["WATERMARK", "NO_SIZE_CHART"],
    }


class FakeLLM:
    """只实现 chat_json 的假客户端；记录每次调用的 messages 与参数。"""

    def __init__(self, response=None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.response


class BombLLM:
    """mock 模式下如果被调用就应该炸掉。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("mock 模式不应调用模型")


# ---------------------------------------------------------------------------
# mock 路径
# ---------------------------------------------------------------------------


def test_mock_reads_database_truth(vision_db: Path) -> None:
    features, meta = inspect(vision_db, "SKU0001")

    assert features.source == "mock"
    assert features.sku_id == "SKU0001"
    assert features.role == "main"
    assert features.prompt_version == PROMPT_VERSION
    # 真值里的观察字段原样回放（问题码除外，问题码按阈值重算）。
    assert features.text_coverage == 0.0
    assert features.scene == "纯白底"

    assert meta["requested"] == "mock"
    assert meta["mode"] == "mock"
    assert meta["degraded"] is False
    assert meta["reason"] == ""
    assert meta["cached"] is False
    assert meta["image_id"] == "SKU0001_main"
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["source"] == "mock"
    assert meta["elapsed_ms"] >= 0


def test_mock_flags_are_english_and_match_derive(vision_db: Path) -> None:
    assert CN_TO_CODE["无尺码表"] == "NO_SIZE_CHART"
    assert CN_TO_CODE["文字占比偏高"] == "TEXT_HEAVY"

    for n in range(1, 21):
        features, _ = inspect(vision_db, f"SKU{n:04d}")
        assert all(code in ISSUE_SPECS for code in features.quality_flags)
        assert all(code == code.upper() for code in features.quality_flags)
        expected = derive_flags(
            text_coverage=features.text_coverage,
            model_present=features.model_present,
            scene=features.scene,
            background=features.background,
            size_chart_present=features.size_chart_present,
            watermark=features.watermark,
            collage=features.collage,
        )
        assert features.quality_flags == expected


def test_missing_sku_raises_lookup_error(vision_db: Path) -> None:
    with pytest.raises(LookupError, match="没有找到 SKU9999 的 main 素材"):
        inspect(vision_db, "SKU9999")


def test_invalid_mode_raises_value_error(vision_db: Path) -> None:
    with pytest.raises(ValueError, match="未知的素材检查模式"):
        inspect(vision_db, "SKU0001", mode="turbo")


def test_auto_mode_without_key_resolves_to_mock(vision_db: Path) -> None:
    features, meta = inspect(vision_db, "SKU0001", mode="auto")

    assert meta["requested"] == "mock"
    assert meta["mode"] == "mock"
    assert features.source == "mock"


# ---------------------------------------------------------------------------
# vlm 路径
# ---------------------------------------------------------------------------


def test_vlm_valid_json_uses_model_and_rederives_flags(vision_db: Path, workdir: Path) -> None:
    image = _image_file(workdir)
    _set_path(vision_db, "SKU0001", "main", image)
    fake = FakeLLM(response=_valid_payload())

    features, meta = inspect(vision_db, "SKU0001", mode="vlm", llm=fake)

    assert features.source == "vlm"
    assert meta["requested"] == "vlm"
    assert meta["mode"] == "vlm"
    assert meta["degraded"] is False
    assert meta["reason"] == ""
    assert meta["cached"] is False
    assert meta["source"] == "vlm"

    # 模型说 watermark=false、text_coverage=0.5，模型自带的 WATERMARK 标签必须被丢弃。
    assert features.watermark is False
    assert "TEXT_HEAVY" in features.quality_flags
    assert "WATERMARK" not in features.quality_flags
    assert features.quality_flags == ["TEXT_HEAVY"]

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["kwargs"]["max_tokens"] == 500
    assert call["kwargs"]["temperature"] == 0.0
    assert call["kwargs"]["model"] == get_settings().llm_model_vision

    messages = call["messages"]
    content = messages[-1]["content"]
    assert messages[-1]["role"] == "user"
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["type"] == "text"
    assert "text_coverage" in content[0]["text"]
    assert "只输出一个 JSON" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["detail"] == "low"
    data_url = content[1]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == _PNG_1X1


@pytest.mark.parametrize("bad", [{"foo": 1}, ["not", "a", "dict"], {"text_coverage": "abc"}])
def test_vlm_garbage_degrades_to_mock(vision_db: Path, workdir: Path, bad) -> None:
    image = _image_file(workdir)
    _set_path(vision_db, "SKU0001", "main", image)
    fake = FakeLLM(response=bad)

    features, meta = inspect(vision_db, "SKU0001", mode="vlm", llm=fake)

    assert features.source == "mock"
    assert meta["mode"] == "mock"
    assert meta["degraded"] is True
    assert meta["reason"]
    assert meta["cached"] is False

    baseline, _ = inspect(vision_db, "SKU0001", mode="mock")
    assert features.model_dump() == baseline.model_dump()


def test_vlm_llm_error_degrades_to_mock(vision_db: Path, workdir: Path) -> None:
    image = _image_file(workdir)
    _set_path(vision_db, "SKU0001", "main", image)
    fake = FakeLLM(error=LLMError("网关超时"))

    features, meta = inspect(vision_db, "SKU0001", mode="vlm", llm=fake)

    assert features.source == "mock"
    assert meta["degraded"] is True
    assert meta["mode"] == "mock"
    assert "模型调用失败" in meta["reason"]
    assert "网关超时" in meta["reason"]


def test_missing_image_file_degrades_with_reason(vision_db: Path, workdir: Path) -> None:
    _set_path(vision_db, "SKU0001", "main", workdir / "not-there.png")
    fake = FakeLLM(response=_valid_payload())

    features, meta = inspect(vision_db, "SKU0001", mode="vlm", llm=fake)

    assert features.source == "mock"
    assert meta["degraded"] is True
    assert "文件" in meta["reason"]
    # 图片都读不到，绝不该再浪费一次模型调用。
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 缓存与模式隔离
# ---------------------------------------------------------------------------


def test_cache_second_call_hits_without_model(vision_db: Path, workdir: Path) -> None:
    image = _image_file(workdir)
    _set_path(vision_db, "SKU0001", "main", image)
    fake = FakeLLM(response=_valid_payload())

    first, first_meta = inspect(vision_db, "SKU0001", mode="vlm", llm=fake)
    second, second_meta = inspect(vision_db, "SKU0001", mode="vlm", llm=fake)

    assert first_meta["cached"] is False
    assert first.source == "vlm"
    assert second_meta["cached"] is True
    assert second_meta["degraded"] is False
    assert second_meta["mode"] == "vlm"
    assert second.source == "cache"
    assert second.prompt_version == PROMPT_VERSION
    assert second.quality_flags == first.quality_flags
    assert second.sku_id == "SKU0001"
    # 缓存命中后模型只被调用过一次。
    assert len(fake.calls) == 1

    with closing(sqlite3.connect(str(vision_db))) as con:
        rows = con.execute("SELECT COUNT(*) FROM vision_cache").fetchone()
    assert rows[0] == 1


def test_mock_mode_never_calls_llm(vision_db: Path) -> None:
    bomb = BombLLM()

    features, meta = inspect(vision_db, "SKU0001", mode="mock", llm=bomb)

    assert bomb.calls == 0
    assert features.source == "mock"
    assert meta["mode"] == "mock"
    assert meta["degraded"] is False


# ---------------------------------------------------------------------------
# 批量、证据与建表
# ---------------------------------------------------------------------------


def test_inspect_many_summary_fields(vision_db: Path) -> None:
    sku_ids = ["SKU0001", "SKU0002", "SKU0003"]

    features_list, meta = inspect_many(vision_db, sku_ids, mode="mock")

    assert len(features_list) == 3
    assert [f.sku_id for f in features_list] == sku_ids
    assert meta["count"] == 3
    assert meta["degraded"] is False
    assert meta["cached"] is False
    assert meta["requested"] == "mock"
    assert meta["mode"] == "mock"
    assert meta["elapsed_ms"] >= 0
    assert meta["items"] == [
        {"sku_id": sku_id, "source": "mock", "degraded": False} for sku_id in sku_ids
    ]
    for key in ("count", "degraded", "cached", "elapsed_ms", "requested", "mode", "items"):
        assert key in meta


def test_evidence_text_contains_chinese_flag(vision_db: Path) -> None:
    features, _ = inspect(vision_db, "SKU0011")

    assert features.size_chart_present is False
    assert "NO_SIZE_CHART" in features.quality_flags
    evidence = features.to_evidence()
    assert evidence["kind"] == "material"
    assert "无尺码表" in evidence["text"]


def test_ensure_cache_table_creates_and_is_idempotent(workdir: Path) -> None:
    db = workdir / "fresh.db"

    ensure_cache_table(db)
    ensure_cache_table(db)

    with closing(sqlite3.connect(str(db))) as con:
        info = list(con.execute("PRAGMA table_info(vision_cache)"))
    columns = [row[1] for row in info]
    primary_key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]

    assert columns == ["image_sha256", "prompt_version", "model", "features_json", "created_at"]
    assert primary_key == ["image_sha256", "prompt_version", "model"]
