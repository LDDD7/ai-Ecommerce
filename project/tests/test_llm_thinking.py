"""关思考的两个约束：默认要把 thinking 关掉；网关不认这个参数时要能自己退让。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.llm import LLMClient, LLMError


class _Completions:
    def __init__(self, error_first: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error_first = error_first

    def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if self.error_first is not None:
            error, self.error_first = self.error_first, None
            raise error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model=kwargs.get("model", "deepseek-flash"),
        )


def _client(completions: _Completions) -> LLMClient:
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LLMClient(base_url="http://x", api_key="sk-test", client=fake, cache=False, max_retries=1)


def test_thinking_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "real")
    from app.core import config

    config.get_settings.cache_clear()
    comp = _Completions()
    _client(comp).chat([{"role": "user", "content": "hi"}])
    assert comp.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    config.get_settings.cache_clear()


def test_thinking_param_dropped_when_gateway_rejects(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "real")
    from app.core import config

    config.get_settings.cache_clear()
    comp = _Completions(error_first=TypeError("unknown field: extra_body"))
    client = _client(comp)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.text == '{"ok":true}'
    assert "extra_body" in comp.calls[0]
    assert "extra_body" not in comp.calls[1], "第二次应当去掉 extra_body 再试"
    config.get_settings.cache_clear()


def test_thinking_on_mode_does_not_send_extra_body(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "real")
    monkeypatch.setenv("LLM_THINKING", "on")
    from app.core import config

    config.get_settings.cache_clear()
    comp = _Completions()
    _client(comp).chat([{"role": "user", "content": "hi"}])
    assert "extra_body" not in comp.calls[0]
    config.get_settings.cache_clear()


def test_no_key_still_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "mock")
    from app.core import config

    config.get_settings.cache_clear()
    with pytest.raises(LLMError):
        _client(_Completions()).chat([{"role": "user", "content": "hi"}])
    config.get_settings.cache_clear()
