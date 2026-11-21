"""应用配置：环境变量 + 默认值。所有模块只从这里读全局设置。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM 网关（OpenAI 兼容）
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model_planner: str = "deepseek-flash"
    llm_model_fast: str = "deepseek-flash"
    llm_model_vision: str = "deepseek-flash"
    # auto: 有 key 走真模型，无 key 自动降级为规则版；real: 强制真调用；mock: 强制规则版
    llm_mode: str = "auto"
    # 单次调用超时与重试：planner 要等模型返回，超时太长会拖垮 8s 的端到端预算，
    # 所以这里默认 12s，重试 1 次；报告叙述用 fast 模型，另给短超时。
    llm_timeout_ms: int = 12_000
    llm_retries: int = 1
    # deepseek-flash 默认会先「思考」：同一条规划问题，开思考 8.7s / 644 tokens（其中 488 是
    # reasoning），关掉只要 2.2s / 279 tokens，JSON 质量还更规矩。off=显式关，on=不干预。
    llm_thinking: str = "off"
    # 报告改写的预算闸：工具跑完时已经花了这么久，就跳过模型改写直接用模板——
    # 验收要求 P95 ≤ 8s，宁可少一段润色，也不能让整轮对话超时。
    report_budget_ms: int = 7_000
    # 反方审查：False=只用规则版（约 0.6s）；True=再让模型挑一遍毛病（只降不升，+2s 左右）。
    # 默认关，因为它会把整体诊断时间推到 7s 以上；需要演示「模型反驳自己」时显式打开。
    critic_llm: bool = False

    # 素材诊断（D9）：auto=有 key 就用视觉模型现算，没有就回放合成图的真值；
    # mock=永远回放（评测、离线演示、CI）；vlm=强制现算（key 不可用时降级并标记 degraded）。
    # 单图 ≤1024 tokens（detail=low 会先缩到 512×512），所以一次检查的成本可以忽略。
    vision_mode: str = "auto"
    vision_detail: str = "low"
    vision_max_side: int = 800

    # 阶段 2 创意工单：local=本地 PIL 改图（默认，零 key 零成本）；
    # dashscope=调百炼生图/改图（要 DASHSCOPE_API_KEY，按张计费）。
    # 注意这把 key 与 LLM_API_KEY 是两家厂商，不能混用。
    creative_provider: str = "local"
    dashscope_api_key: str = ""
    creative_image_model: str = "wan2.2-t2i-flash"
    creative_edit_model: str = "wan2.5-i2i-preview"
    creative_adapter: str = ""

    # 检索（P2 用）
    embed_mode: str = "local"
    embed_model_path: str = ""
    embed_dim: int = 1024
    rerank_mode: str = "rule"
    rerank_model_path: str = ""
    rerank_top_n: int = 6
    rerank_timeout_ms: int = 3500

    # 应用
    app_env: str = "dev"
    db_path: str = "data/store.db"
    log_level: str = "INFO"

    # 登录鉴权：password=必须登录（默认，正式使用）；off=不校验（本地调试/测试）。
    # 首次启动若 users 表为空，会用 AUTH_ADMIN_PASSWORD 建 admin，没配就随机生成并打印。
    auth_mode: str = "password"
    auth_admin_password: str = ""
    session_ttl_hours: int = 168
    cookie_secure: bool = False
    # 演示基准日：首屏与默认诊断都以这天为 as_of（2026-10-20 是干净窗口，
    # 避开 R7 大促后透支窗口，见 spec §15）。超出数据范围时自动回落到最新一天。
    demo_as_of: str = "2026-11-17"

    # 数据规模（可调参数，见 spec §2.3）
    data_sku_count: int = 300
    data_days: int = 180
    data_orders_target: int = 200_000
    data_seed: int = 42
    data_end_date: str = "2026-11-18"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key.strip())

    @property
    def llm_active(self) -> str:
        """实际生效的 LLM 模式：real 或 mock。"""
        if self.llm_mode == "mock":
            return "mock"
        if self.llm_mode == "real":
            return "real"
        return "real" if self.llm_enabled else "mock"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
