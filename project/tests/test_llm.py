"""``app.core.llm`` 的离线测试：全部使用注入的假客户端，不发任何网络请求。"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import llm
from app.core.config import get_settings
from app.core.llm import (
    LLMClient,
    LLMError,
    llm_available,
    usage_stats,
)

# 本机 %TEMP% 目录 ACL 异常，测试产物统一放工作区 .tmp 下（见 tests/conftest.py）。
_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "test_llm"


# ---------------------------------------------------------------------------
# 夹具与假客户端
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _llm_test_env(monkeypatch: pytest.MonkeyPatch):
    """每个测试都强制 real 模式 + 假 key，并清空单例与统计。"""
    monkeypatch.setenv("LLM_MODE", "real")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    get_settings.cache_clear()
    llm.reset_client()
    yield
    llm.reset_client()
    get_settings.cache_clear()


@pytest.fixture
def workdir() -> Path:
    """工作区内的独立临时目录（避免使用不可靠的 %TEMP%）。"""
    path = _TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def cache_db(workdir: Path) -> str:
    """按生产库结构建一个空的 llm_cache 表。"""
    path = workdir / "cache.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE llm_cache (
                key         TEXT PRIMARY KEY,
                response    TEXT NOT NULL,
                tokens      INTEGER NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


def _response(
    text: str = "ok",
    *,
    model: str = "fake-model",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> SimpleNamespace:
    """构造一个与 openai SDK 响应结构一致的假响应。"""
    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


class FakeClient:
    """假 OpenAI 客户端：按顺序返回 responses 里的响应或抛出异常。

    最后一个元素会被重复使用（例如「一直超时」的用例）。
    """

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs) -> SimpleNamespace:
        self.calls.append(kwargs)
        item = (
            self._responses.pop(0)
            if len(self._responses) > 1
            else self._responses[0]
        )
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]


class FakeTimeoutError(Exception):
    """类名含 Timeout，用于验证通用重试判定。"""


class FakeAuthenticationError(Exception):
    """类名含 Authentication，用于验证认证失败不重试。"""


def _make_client(fake: FakeClient, *, db_path: str | None = None, max_retries: int = 2) -> LLMClient:
    return LLMClient(
        base_url="http://fake.local/v1",
        api_key="sk-test",
        timeout_s=2.0,
        max_retries=max_retries,
        db_path=db_path,
        client=fake,
    )


_MESSAGES = [{"role": "user", "content": "给我一个诊断结论"}]


# ---------------------------------------------------------------------------
# 基础行为
# ---------------------------------------------------------------------------


def test_no_api_key_raises_llm_error():
    fake = FakeClient([_response()])
    client = LLMClient(base_url="http://fake.local/v1", api_key="", client=fake)

    with pytest.raises(LLMError) as excinfo:
        client.chat(_MESSAGES)

    assert "LLM 未配置" in str(excinfo.value)
    assert fake.calls == []


def test_mock_mode_raises_even_with_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_MODE", "mock")
    get_settings.cache_clear()
    fake = FakeClient([_response()])
    client = _make_client(fake)

    with pytest.raises(LLMError) as excinfo:
        client.chat(_MESSAGES)

    assert "LLM 未配置" in str(excinfo.value)
    assert fake.calls == []


def test_chat_returns_fields_and_accumulates_usage():
    fake = FakeClient(
        [_response("你好", prompt_tokens=12, completion_tokens=7)]
    )
    client = _make_client(fake)

    result = client.chat(_MESSAGES, model="fast-x", max_tokens=256)

    assert result.text == "你好"
    assert result.model == "fake-model"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 7
    assert result.elapsed_ms >= 0.0
    assert result.cached is False
    assert fake.calls[0]["model"] == "fast-x"
    assert fake.calls[0]["max_tokens"] == 256

    assert usage_stats() == {
        "calls": 1,
        "cache_hits": 0,
        "prompt_tokens": 12,
        "completion_tokens": 7,
    }
    assert llm_available() is True

    dumped = result.as_dict()
    assert set(dumped) == {
        "text",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "elapsed_ms",
        "cached",
    }
    assert dumped["elapsed_ms"] == round(result.elapsed_ms, 1)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def test_second_identical_chat_hits_cache(cache_db: str):
    fake = FakeClient(
        [_response("缓存内容", prompt_tokens=9, completion_tokens=4)]
    )
    client = _make_client(fake, db_path=cache_db)

    first = client.chat(_MESSAGES)
    second = client.chat(_MESSAGES)

    assert first.cached is False
    assert second.cached is True
    assert second.text == "缓存内容"
    assert second.prompt_tokens == 0
    assert second.completion_tokens == 0
    assert len(fake.calls) == 1

    stats = usage_stats()
    assert stats["calls"] == 1
    assert stats["cache_hits"] == 1
    assert stats["prompt_tokens"] == 9
    assert stats["completion_tokens"] == 4


def test_use_cache_false_bypasses_cache(cache_db: str):
    fake = FakeClient([_response("每次都调用")])
    client = _make_client(fake, db_path=cache_db)

    first = client.chat(_MESSAGES, use_cache=False)
    second = client.chat(_MESSAGES, use_cache=False)

    assert first.cached is False
    assert second.cached is False
    assert len(fake.calls) == 2
    assert usage_stats()["calls"] == 2
    assert usage_stats()["cache_hits"] == 0


def test_cache_io_errors_are_swallowed(workdir: Path):
    # 父目录不存在 -> sqlite 打不开；读写异常都应被吞掉。
    bad_path = workdir / "no_such_dir" / "deep" / "cache.db"
    fake = FakeClient([_response("仍然成功")])
    client = _make_client(fake, db_path=str(bad_path))

    result = client.chat(_MESSAGES)

    assert result.text == "仍然成功"
    assert result.cached is False


# ---------------------------------------------------------------------------
# 重试与异常语义
# ---------------------------------------------------------------------------


def test_retry_once_after_timeout_then_success(monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fake = FakeClient([FakeTimeoutError("slow"), _response("重试成功")])
    client = _make_client(fake, max_retries=2)

    result = client.chat(_MESSAGES)

    assert result.text == "重试成功"
    assert len(fake.calls) == 2
    assert len(sleeps) == 1
    assert sleeps[0] > 0


def test_retry_exhausted_raises_llm_error(monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fake = FakeClient([FakeTimeoutError("always slow")])
    client = _make_client(fake, max_retries=2)

    with pytest.raises(LLMError) as excinfo:
        client.chat(_MESSAGES)

    assert "重试" in str(excinfo.value)
    assert len(fake.calls) == 3
    assert len(sleeps) == 2


def test_authentication_error_is_not_retried(monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fake = FakeClient([FakeAuthenticationError("bad key")])
    client = _make_client(fake, max_retries=3)

    with pytest.raises(LLMError) as excinfo:
        client.chat(_MESSAGES)

    assert "key 无效" in str(excinfo.value)
    assert len(fake.calls) == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# chat_json 解析
# ---------------------------------------------------------------------------


def test_chat_json_parses_bare_json():
    payload = '{"intent": "anomaly", "confidence": 0.9}'
    fake = FakeClient([_response(payload)])
    client = _make_client(fake)

    data = client.chat_json(_MESSAGES)

    assert data == {"intent": "anomaly", "confidence": 0.9}
    sent = fake.calls[0]["messages"]
    assert "只输出 JSON" in sent[-1]["content"]
    assert "response_format" not in fake.calls[0]


def test_chat_json_parses_fenced_json():
    # 围栏里是 list，同时验证「list 包成 {"items": [...]}」的约定。
    fake = FakeClient([_response('```json\n[1, 2, 3]\n```')])
    client = _make_client(fake)

    assert client.chat_json(_MESSAGES) == {"items": [1, 2, 3]}


def test_chat_json_parses_json_with_surrounding_text():
    fake = FakeClient([_response('好的，结果如下：\n{"ok": true}\n以上。')])
    client = _make_client(fake)

    assert client.chat_json(_MESSAGES) == {"ok": True}


def test_chat_json_invalid_text_raises_llm_error():
    fake = FakeClient([_response("抱歉，我无法判断。")])
    client = _make_client(fake)

    with pytest.raises(LLMError) as excinfo:
        client.chat_json(_MESSAGES)

    assert "JSON" in str(excinfo.value)
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 可用性
# ---------------------------------------------------------------------------


def test_llm_not_available_without_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_MODE", "auto")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()

    assert llm_available() is False
