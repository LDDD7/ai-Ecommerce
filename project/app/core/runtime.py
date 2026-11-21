"""运行时配置覆盖层：``.env`` 默认值之上，再叠一层「页面上填的」值。

优先级：**数据库覆盖 > .env > 代码默认值**。

为什么需要它：``get_settings()`` 是 ``lru_cache`` 的，进程启动后 key 就固化了，
在页面上改 key 不重启就永远不生效。这里把「可覆盖字段」抽出来按需读，
保存设置时 ``refresh()`` 一下，下一次请求就用新值。

注意：这里只做「读」和「合并」，写入在 ``settings_store``，接口在 ``api/routes``。
"""

from __future__ import annotations

import threading

from app.core.config import get_settings
from app.core.settings_store import FIELD_LABELS, OVERRIDE_FIELDS, SECRET_FIELDS, mask, read_all

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, str]] = {}


def _db_path() -> str:
    return get_settings().db_path


def overrides() -> dict[str, str]:
    """当前生效的覆盖值（按 db_path 缓存，refresh 后失效）。"""
    path = _db_path()
    with _LOCK:
        cached = _CACHE.get(path)
    if cached is not None:
        return cached
    data = read_all(path)
    with _LOCK:
        _CACHE[path] = data
    return data


def value(name: str, default: str | None = None) -> str:
    """取单字段生效值：覆盖值非空则用它，否则用 .env 的值。"""
    override = overrides().get(name, "").strip()
    if override:
        return override
    if default is not None:
        return str(getattr(get_settings(), name, default) or default)
    return str(getattr(get_settings(), name, "") or "")


def llm_active() -> str:
    """real / mock —— 与 ``Settings.llm_active`` 同义，但会尊重页面上的覆盖。"""
    mode = value("llm_mode") or "auto"
    if mode == "mock":
        return "mock"
    if mode == "real":
        return "real"
    return "real" if value("llm_api_key").strip() else "mock"


def model_for(kind: str) -> str:
    """planner / fast / vision 三档模型名（页面可分别覆盖）。"""
    return value(f"llm_model_{kind}") or "deepseek-flash"


def creative_provider() -> str:
    return value("creative_provider") or "local"


def dashscope_key() -> str:
    return value("dashscope_api_key").strip()


def refresh() -> None:
    """保存设置后调用：丢掉覆盖缓存，并把 LLM 单例客户端作废。"""
    with _LOCK:
        _CACHE.clear()
    try:  # 循环 import：llm 会反过来 import 本模块
        from app.core.llm import reset_client

        reset_client()
    except Exception:  # pragma: no cover - 配置阶段不该因为清理失败而报错
        pass


def snapshot() -> dict:
    """给 ``GET /api/settings`` 用的只读快照：明文一律换成掩码。"""
    settings = get_settings()
    ov = overrides()
    fields: dict[str, dict] = {}
    for name in OVERRIDE_FIELDS:
        raw = ov.get(name, "").strip()
        env_value = str(getattr(settings, name, "") or "")
        effective = raw or env_value
        if name in SECRET_FIELDS:
            fields[name] = {
                "label": FIELD_LABELS.get(name, name),
                "secret": True,
                "value": "",
                "masked": mask(effective),
                "has_value": bool(effective.strip()),
                "source": "db" if raw else ("env" if env_value.strip() else "none"),
            }
        else:
            fields[name] = {
                "label": FIELD_LABELS.get(name, name),
                "secret": False,
                "value": effective,
                "masked": effective,
                "has_value": bool(effective.strip()),
                "source": "db" if raw else ("env" if env_value.strip() else "none"),
            }
    return {
        "fields": fields,
        "overrides": sorted(ov.keys()),
        "llm": {
            "active": llm_active(),
            "base_url": value("llm_base_url"),
            "models": {
                "planner": model_for("planner"),
                "fast": model_for("fast"),
                "vision": model_for("vision"),
            },
        },
        "creative": {
            "provider": creative_provider(),
            "has_key": bool(dashscope_key()),
            "image_model": value("creative_image_model"),
            "edit_model": value("creative_edit_model"),
            "adapter": value("creative_adapter"),
        },
    }
