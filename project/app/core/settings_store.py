"""运行时设置持久化：把「页面上填的 key / 地址 / 模型」存在本地 SQLite。

设计约束（很重要，都是有意的）：

1. **key 只进不出**。``mask()`` 是唯一对外暴露的方式；``GET /api/settings``
   永远不返回明文。写库之后连日志里都不该出现明文。
2. **不写进 .env**。.env 是「出厂默认值」，这里是「用户覆盖值」；
   两者分开的好处是：清空覆盖就能一键回到 .env，且 .env 可以安全提交示例。
3. **.db 已被 .gitignore**，所以填了 key 也不会被推到 GitHub。
4. 表用「键值对 + 更新时间」，不建模型类：字段会随功能增加，改代码比迁移表便宜。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 所有允许被覆盖的字段（顺序即打印顺序）。新增字段时只改这里 + runtime.py。
OVERRIDE_FIELDS: tuple[str, ...] = (
    "llm_base_url",
    "llm_api_key",
    "llm_model_planner",
    "llm_model_fast",
    "llm_model_vision",
    "llm_mode",
    "creative_provider",
    "dashscope_api_key",
    "creative_image_model",
    "creative_edit_model",
    "creative_adapter",
)

# 需要掩码的字段：读接口只给前 6 后 4，绝不回明文。
SECRET_FIELDS: frozenset[str] = frozenset({"llm_api_key", "dashscope_api_key"})

# 服务商预设：前端下拉用，后端只负责发出去，不做任何校验之外的事情。
PROVIDERS: tuple[dict, ...] = (
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-flash", "deepseek-chat", "deepseek-reasoner"],
        "note": "本项目默认厂商；规划与报告都用 deepseek-flash 最省。",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼（OpenAI 兼容模式）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.8-flash", "qwen3.7-plus", "qwen4-max", "qwen3.8-omni-flash"],
        "note": "兼容模式可以当 OpenAI 用；生图要走另一套异步接口，在下面的「生图」里配。",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-32B"],
        "note": "国内直连、价格低，适合当备用网关。",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "note": "需要海外网络；国内演示不建议。",
    },
    {
        "id": "custom",
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "models": [],
        "note": "任何 OpenAI 兼容网关：vLLM、one-api、Ollama 的 /v1 都行。",
    },
)

CREATIVE_PROVIDERS: tuple[dict, ...] = (
    {
        "id": "local",
        "label": "本地 PIL 改图（默认，不需要 key）",
        "models": [],
        "note": "按问题码在现有主图上改：补尺码表、压文字、换底、去水印、拆拼接。",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼 · 生图",
        "models": [
            "wan2.2-t2i-flash",
            "wan2.5-t2i-preview",
            "wan2.6-t2i",
            "wan2.5-i2i-preview",
            "qwen-image-3.0-pro",
        ],
        "note": "需要 DASHSCOPE_API_KEY；异步任务接口，按张计费（约 ¥0.2/张）。",
    },
)

# 展示用的中文名与分组，前端直接用，避免在 tsx 里再抄一份映射。
FIELD_LABELS: dict[str, str] = {
    "llm_base_url": "API 地址",
    "llm_api_key": "对话模型 Key",
    "llm_model_planner": "规划模型",
    "llm_model_fast": "快模型（写报告）",
    "llm_model_vision": "视觉模型（看主图）",
    "llm_mode": "调用模式",
    "creative_provider": "生图方式",
    "dashscope_api_key": "生图 Key（百炼）",
    "creative_image_model": "文生图模型",
    "creative_edit_model": "图生图模型",
    "creative_adapter": "外部创意服务",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_setting (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """只读写设置表；用可写连接（设置是唯一允许写库的模块之一）。"""
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(SCHEMA)


def read_all(db_path: str | Path) -> dict[str, str]:
    """读出全部覆盖值；表不存在或库不存在时返回空字典（不抛错）。"""
    try:
        with _connect(db_path) as conn:
            rows = conn.execute("SELECT name, value FROM app_setting").fetchall()
    except sqlite3.Error:
        return {}
    return {row["name"]: row["value"] for row in rows if row["name"] in OVERRIDE_FIELDS}


def write_many(db_path: str | Path, values: dict[str, str]) -> dict[str, str]:
    """写入一批覆盖值（只认白名单字段），返回写入后的全部覆盖值。

    空字符串的语义是「删除这条覆盖」，不是「把值清成空」——否则用户
    清空输入框后反而会把 .env 的 key 顶掉。
    """
    ensure_table(db_path)
    stamp = _now()
    with _connect(db_path) as conn:
        for name, value in values.items():
            if name not in OVERRIDE_FIELDS:
                continue
            text = (value or "").strip()
            if text:
                conn.execute(
                    "INSERT INTO app_setting(name, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (name, text, stamp),
                )
            else:
                conn.execute("DELETE FROM app_setting WHERE name = ?", (name,))
    return read_all(db_path)


def clear_all(db_path: str | Path) -> None:
    """清空全部覆盖值 = 回到 .env 默认。"""
    try:
        ensure_table(db_path)
        with _connect(db_path) as conn:
            conn.execute("DELETE FROM app_setting")
    except sqlite3.Error:
        pass


def mask(secret: str | None) -> str:
    """掩码：≥12 字符给前 6 后 4，否则全星号。空值返回空串。"""
    text = (secret or "").strip()
    if not text:
        return ""
    if len(text) < 12:
        return "*" * len(text)
    return f"{text[:6]}…{text[-4:]}"


def validate(values: dict[str, str]) -> list[str]:
    """字段级校验，返回中文错误列表；空值不校验（代表「删除覆盖」）。"""
    errors: list[str] = []
    base = (values.get("llm_base_url") or "").strip()
    if base and not (base.startswith("http://") or base.startswith("https://")):
        errors.append("API 地址必须以 http:// 或 https:// 开头")
    if len(base) > 300:
        errors.append("API 地址过长（≤300 字符）")
    adapter = (values.get("creative_adapter") or "").strip()
    if adapter and not (adapter.startswith("http://") or adapter.startswith("https://")):
        errors.append("外部创意服务地址必须以 http:// 或 https:// 开头")
    if (values.get("llm_mode") or "").strip() not in ("", "auto", "real", "mock"):
        errors.append("调用模式只能是 auto / real / mock")
    if (values.get("creative_provider") or "").strip() not in ("", "local", "dashscope"):
        errors.append("生图方式只能是 local / dashscope")
    for name in ("llm_model_planner", "llm_model_fast", "llm_model_vision",
                 "creative_image_model", "creative_edit_model"):
        text = (values.get(name) or "").strip()
        if text and (len(text) > 120 or any(ch.isspace() for ch in text)):
            errors.append(f"{FIELD_LABELS.get(name, name)}不合法（不能含空格、≤120 字符）")
    for name in SECRET_FIELDS:
        text = (values.get(name) or "").strip()
        if text and len(text) < 8:
            errors.append(f"{FIELD_LABELS.get(name, name)}太短（看起来不像有效的 key）")
    return errors
