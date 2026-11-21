"""LLM 网关：对 OpenAI 兼容接口的统一封装（chat / chat_json）。

设计原则：
1. 单例客户端：进程内共用一个 ``LLMClient``，配置变更后调用 :func:`reset_client`。
2. 本地缓存：相同请求命中 SQLite 的 ``llm_cache``，省钱且结果稳定。
3. 降级原则：任何失败都抛 LLMError，由调用方决定回退，本模块绝不返回半成品
   （不返回空串、不返回猜测内容）。

重试策略：认证失败不重试；超时、连接错误、429、5xx 做指数退避，
最多重试 ``max_retries`` 次（不含首次调用）。SDK 自身的重试被显式关闭
（``max_retries=0``），避免两层重试叠加放大延迟。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import openai
from openai import OpenAI

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 对外统一的“未配置”提示，调用方可直接展示给用户。
NO_KEY_MESSAGE = "LLM 未配置：请在 project/.env 里填 LLM_API_KEY"
# chat_json 追加在提示词末尾的约束。
JSON_ONLY_INSTRUCTION = "只输出 JSON，不要解释、不要 markdown 代码块。"

_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 8.0
# 单独抽出 sleep，测试可 monkeypatch，避免真的等待。
_sleep = time.sleep


class LLMError(RuntimeError):
    """LLM 调用失败。调用方捕获后自行决定回退到规则版或提示用户。"""


@dataclass(slots=True)
class ChatResult:
    """一次 chat 调用的结果（含缓存标记）。"""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_ms: float
    cached: bool = False

    def as_dict(self) -> dict:
        """转为可 JSON 序列化的字典，``elapsed_ms`` 保留 1 位小数。"""
        return {
            "text": self.text,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "cached": self.cached,
        }


# ---------------------------------------------------------------------------
# 进程级统计
# ---------------------------------------------------------------------------

_STATS_LOCK = threading.Lock()
_STATS: dict[str, int] = {
    "calls": 0,
    "cache_hits": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
}


def usage_stats() -> dict:
    """返回累计用量：``calls`` 只统计真实模型调用，缓存命中单列在 ``cache_hits``。"""
    with _STATS_LOCK:
        return dict(_STATS)


def _record_success(prompt_tokens: int, completion_tokens: int) -> None:
    with _STATS_LOCK:
        _STATS["calls"] += 1
        _STATS["prompt_tokens"] += prompt_tokens
        _STATS["completion_tokens"] += completion_tokens


def _record_cache_hit() -> None:
    with _STATS_LOCK:
        _STATS["cache_hits"] += 1


# ---------------------------------------------------------------------------
# 单例客户端
# ---------------------------------------------------------------------------

_CLIENT_LOCK = threading.Lock()
_client: LLMClient | None = None


def llm_available() -> bool:
    """当前配置是否真的能调模型（``llm_active == "real"``）。

    走 :mod:`app.core.runtime` 而不是直接读 settings：页面上的「设置」页可以
    覆盖 key / 地址 / 模式，覆盖后必须立刻生效，不能等重启。
    """
    from app.core import runtime

    return runtime.llm_active() == "real"


def get_client() -> LLMClient:
    """返回进程内单例客户端；首次调用时按「生效配置」构造。

    生效配置 = 数据库覆盖 > .env；保存设置时 ``runtime.refresh()`` 会调
    :func:`reset_client`，所以改完 key 下一次请求就能用新值。
    """
    global _client
    if _client is None:
        with _CLIENT_LOCK:
            if _client is None:
                from app.core import runtime

                settings = get_settings()
                _client = LLMClient(
                    base_url=runtime.value("llm_base_url"),
                    api_key=runtime.value("llm_api_key"),
                    db_path=settings.db_path or None,
                    cache=True,
                    # 8s 端到端预算下不能给长超时：宁可降级也不能让用户在页面上干等
                    timeout_s=settings.llm_timeout_ms / 1000,
                    max_retries=settings.llm_retries,
                )
    return _client


def reset_client() -> None:
    """清空单例与统计。配置变化或测试之间调用。"""
    global _client
    with _CLIENT_LOCK:
        _client = None
    with _STATS_LOCK:
        for key in _STATS:
            _STATS[key] = 0


# ---------------------------------------------------------------------------
# 异常分类
# ---------------------------------------------------------------------------

_RETRYABLE_OPENAI_ERRORS = tuple(
    cls
    for cls in (
        getattr(openai, "APITimeoutError", None),
        getattr(openai, "APIConnectionError", None),
        getattr(openai, "RateLimitError", None),
        getattr(openai, "InternalServerError", None),
    )
    if isinstance(cls, type)
)

# 异常类名里的通用特征：注入的假客户端不会抛 openai 的类型，只能按名字识别。
_RETRY_NAME_HINTS = (
    "timeout",
    "timedout",
    "connection",
    "connecterror",
    "ratelimit",
    "rate_limit",
    "internalserver",
    "internal_server",
    "serviceunavailable",
    "service_unavailable",
    "badgateway",
    "bad_gateway",
    "gatewaytimeout",
    "gateway_timeout",
    "overloaded",
)

_AUTH_NAME_HINTS = ("authentication", "permissiondenied", "invalidapikey")


def _status_of(exc: BaseException) -> int | None:
    """尽量从异常上取 HTTP 状态码。"""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_auth_error(exc: BaseException) -> bool:
    """认证/鉴权失败：不重试，直接降级。"""
    if isinstance(exc, openai.AuthenticationError):
        return True
    name = type(exc).__name__.lower()
    if any(hint in name for hint in _AUTH_NAME_HINTS):
        return True
    return _status_of(exc) in (401, 403)


def _is_retryable(exc: BaseException) -> bool:
    """超时、连接错误、429、5xx 可重试；其它异常不重试。"""
    if _is_auth_error(exc):
        return False
    if _RETRYABLE_OPENAI_ERRORS and isinstance(exc, _RETRYABLE_OPENAI_ERRORS):
        return True
    status = _status_of(exc)
    if status == 429 or (status is not None and 500 <= status < 600):
        return True
    name = type(exc).__name__.lower()
    return any(hint in name for hint in _RETRY_NAME_HINTS)


# ---------------------------------------------------------------------------
# 缓存与 JSON 工具
# ---------------------------------------------------------------------------


def _thinking_unsupported(exc: BaseException) -> bool:
    """判断异常是不是「不认识 thinking 参数」（换网关时最常见）。"""
    text = str(exc).lower()
    return any(word in text for word in ("thinking", "extra_body", "extra body", "unknown field", "unrecognized"))


def _make_cache_key(
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    response_format: dict | None,
    variant: str = "",
) -> str:
    """按契约拼接缓存 key（字段顺序与分隔符不可随意改）。

    ``variant`` 用来区分「关思考/开思考」这类会影响输出的模式：模式不同不能共用缓存，
    否则开着思考写进去的空正文会被关思考的调用命中。
    """
    payload = (
        model
        + "\x1f"
        + json.dumps(messages, ensure_ascii=False, sort_keys=True)
        + "\x1f"
        + str(temperature)
        + "\x1f"
        + str(max_tokens)
        + "\x1f"
        + json.dumps(response_format or {}, sort_keys=True)
        + "\x1f"
        + variant
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+-]+)?\s*(.*?)```", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """去掉 markdown 代码围栏，返回围栏内的内容；没有围栏则原样返回。"""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def _preview(text: str, limit: int = 200) -> str:
    """错误信息里只带短预览，避免把整段回复写进日志。"""
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_json_object(text: str) -> dict:
    """从模型回复中提取 JSON 对象。

    依次尝试：直接 loads → 去代码围栏 → 截取第一个 ``{`` 到最后一个 ``}``。
    解析出 list 时包成 ``{"items": [...]}``；其它非对象结果一律报错。
    """
    raw = (text or "").strip()
    candidates: list[str] = [raw]

    fenced = _strip_code_fence(raw)
    if fenced != raw:
        candidates.append(fenced)

    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}

    raise LLMError(f"LLM 未返回可解析的 JSON 对象：{_preview(raw)}")


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时兼容对象属性与 dict 取值。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class LLMClient:
    """OpenAI 兼容网关客户端。

    ``client`` 参数用于测试注入假客户端；不传时按配置构造真正的 OpenAI 客户端。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float = 20.0,
        max_retries: int = 2,
        db_path: str | None = None,
        cache: bool = True,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = (api_key or "").strip()
        self._timeout_s = float(timeout_s)
        self._max_retries = max(0, int(max_retries))
        self._db_path = str(db_path) if db_path else None
        self._cache = bool(cache)

        if client is not None:
            self._client = client
        elif self._api_key:
            # 重试由本模块控制，SDK 侧关闭，避免两层重试叠加。
            self._client = OpenAI(
                base_url=base_url,
                api_key=self._api_key,
                timeout=self._timeout_s,
                max_retries=0,
            )
        else:
            # 没 key 时先不构造（OpenAI 会直接报错），留给 chat 抛统一 LLMError。
            self._client = None

    # -- 缓存读写：任何异常都吞掉，缓存坏了不能影响主流程 -----------------

    def _cache_get(self, key: str) -> str | None:
        assert self._db_path is not None  # 内部调用前已判断
        try:
            with closing(sqlite3.connect(self._db_path, timeout=3.0)) as conn:
                row = conn.execute(
                    "SELECT response FROM llm_cache WHERE key = ?", (key,)
                ).fetchone()
            if row and isinstance(row[0], str):
                return row[0]
        except Exception as exc:  # noqa: BLE001 - 缓存故障必须静默
            logger.debug("读取 LLM 缓存失败（已忽略）：%s", exc)
        return None

    def _cache_put(self, key: str, response: str, tokens: int) -> None:
        assert self._db_path is not None
        try:
            with closing(sqlite3.connect(self._db_path, timeout=3.0)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache "
                    "(key, response, tokens, created_at) VALUES (?, ?, ?, ?)",
                    (
                        key,
                        response,
                        int(tokens),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - 缓存故障必须静默
            logger.debug("写入 LLM 缓存失败（已忽略）：%s", exc)

    # -- 对外接口 ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
        response_format: dict | None = None,
        use_cache: bool = True,
        timeout_s: float | None = None,
    ) -> ChatResult:
        """调用模型并返回文本结果。

        失败语义见模块 docstring：认证失败、不可重试异常、重试耗尽都抛
        :class:`LLMError`，不返回半成品。
        """
        if not self._api_key:
            raise LLMError(NO_KEY_MESSAGE)

        settings = get_settings()
        from app.core import runtime

        if runtime.llm_active() == "mock":
            raise LLMError(NO_KEY_MESSAGE)

        model = model or runtime.model_for("fast")
        started = time.perf_counter()

        # 1) 查缓存
        cache_key: str | None = None
        if self._cache and self._db_path and use_cache:
            try:
                cache_key = _make_cache_key(
                    model,
                    messages,
                    temperature,
                    max_tokens,
                    response_format,
                    "" if getattr(settings, "llm_thinking", "off") == "off" else "think",
                )
            except Exception as exc:  # noqa: BLE001 - 序列化失败就当没缓存
                logger.debug("生成 LLM 缓存 key 失败（已忽略）：%s", exc)
                cache_key = None

            if cache_key:
                cached_text = self._cache_get(cache_key)
                if cached_text is not None:
                    _record_cache_hit()
                    # 命中不消耗 token，因此本次结果的 token 计数为 0。
                    return ChatResult(
                        text=cached_text,
                        model=model,
                        prompt_tokens=0,
                        completion_tokens=0,
                        elapsed_ms=(time.perf_counter() - started) * 1000.0,
                        cached=True,
                    )

        if self._client is None:
            raise LLMError(NO_KEY_MESSAGE)

        # 2) 真正调用（带重试）
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if timeout_s is not None:
            # 只有显式覆盖时才逐次传；默认超时已设在客户端构造参数上。
            kwargs["timeout"] = float(timeout_s)
        if getattr(settings, "llm_thinking", "off") == "off":
            # 关掉思考链：规划/改写这类任务不需要推理，开着要多花 4 倍时间与 token。
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        response: Any = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.chat.completions.create(**kwargs)
                break
            except Exception as exc:  # noqa: BLE001 - 统一翻译成 LLMError
                if "extra_body" in kwargs and _thinking_unsupported(exc):
                    # 换一家网关（通义/硅基）可能不认 thinking 参数：去掉再试一次，
                    # 慢一点但能跑；这条日志值得看，说明该换 LLM_THINKING=on 了。
                    kwargs.pop("extra_body", None)
                    logger.warning(
                        "网关不接受 thinking 参数，已去掉后重试一次（建议设 LLM_THINKING=on）：%s", exc
                    )
                    continue
                if _is_auth_error(exc):
                    raise LLMError(
                        f"LLM 认证失败：API key 无效（{type(exc).__name__}: {exc}）"
                    ) from exc
                if not _is_retryable(exc):
                    raise LLMError(
                        f"LLM 调用失败：{type(exc).__name__}: {exc}"
                    ) from exc
                if attempt >= self._max_retries:
                    raise LLMError(
                        f"LLM 调用失败（已重试 {self._max_retries} 次）："
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                delay = min(_BACKOFF_BASE_S * (2**attempt), _BACKOFF_MAX_S)
                logger.warning(
                    "LLM 调用失败，%.1fs 后重试（%d/%d）：%s",
                    delay,
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                _sleep(delay)

        if response is None:  # 理论不可达，防御性兜底
            raise LLMError("LLM 调用失败：未获得响应")

        # 3) 解析响应
        try:
            choice = response.choices[0]
            text = _field(_field(choice, "message"), "content") or ""
        except Exception as exc:  # noqa: BLE001 - 结构异常也是降级信号
            raise LLMError(f"LLM 返回结构异常：{exc}") from exc

        if not isinstance(text, str):
            text = str(text)

        usage = _field(response, "usage")
        prompt_tokens = int(_field(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(_field(usage, "completion_tokens", 0) or 0)
        actual_model = _field(response, "model") or model
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        # 4) 记账 + 回写缓存（只有成功结果才写）
        _record_success(prompt_tokens, completion_tokens)
        if cache_key:
            self._cache_put(cache_key, text, prompt_tokens + completion_tokens)

        return ChatResult(
            text=text,
            model=actual_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            cached=False,
        )

    def chat_json(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict:
        """要求模型只输出 JSON，并解析为 dict（list 会包成 ``{"items": [...]}``）。"""
        prepared = [dict(message) for message in messages]
        if prepared and isinstance(prepared[-1].get("content"), str):
            prepared[-1]["content"] = (
                prepared[-1]["content"].rstrip() + "\n\n" + JSON_ONLY_INSTRUCTION
            )
        else:
            prepared.append({"role": "user", "content": JSON_ONLY_INSTRUCTION})

        result = self.chat(
            prepared,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _parse_json_object(result.text)
