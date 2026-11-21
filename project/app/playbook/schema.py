"""策略卡（Playbook）数据结构与校验规则。

一张卡 = 一个「针对某类根因、可执行、可衡量」的动作模板。卡是 YAML 写的，
加载时用 pydantic 严格校验：多一个字段就报错，避免 agent 写卡时夹带自由发挥。

字段约定（与 spec §8 一致）：

    card_id         PB-R4-01，PB 前缀 + 根因码 + 两位序号
    root_causes     这张卡治哪些根因（R1..R10），可多个
    metrics         这张卡主要改善哪些指标（必须是 catalog.yaml 里的名字）
    risk            0 无需审批（内容/自检）｜1 需人工审批（投放/素材）｜2 高风险（价格/资金）
    scope           作用域：sku / style / channel / store
    action_type     落到执行层的动作类型，必须是 5 类之一
    preconditions   触发条件（人读，也是检索语料）
    steps           具体操作步骤，至少两步
    expected_effect 预期改善：指标 + 方向 + 相对幅度区间 + 生效窗口
    success_metric  复盘时看哪个指标
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 根因码 → 中文名（与 anomaly_gt / hypothesis.CODE_TITLES 保持一致）
ROOT_CAUSES: dict[str, str] = {
    "R1": "竞品降价战",
    "R2": "主图素材衰减",
    "R3": "热销断货",
    "R4": "尺码与描述不符",
    "R5": "投放 ROI 恶化",
    "R6": "差评爆发",
    "R7": "大促后透支",
    "R8": "竞争加剧·流量下滑",
    "R9": "价格带错配",
    "R10": "物流时效恶化",
}

ACTION_TYPES = ("adjust_price", "adjust_bid", "swap_creative", "issue_coupon", "restock_alert")
SCOPES = ("sku", "style", "channel", "store")


class ExpectedEffect(BaseModel):
    """预期改善幅度。区间用相对幅度（0.15 = 改善 15%），不承诺绝对金额。"""

    model_config = {"extra": "forbid"}

    metric: str
    direction: Literal["down", "up"]
    range: tuple[float, float]
    window_days: int = Field(default=14, ge=1, le=90)

    @model_validator(mode="after")
    def _check_range(self) -> "ExpectedEffect":
        low, high = self.range
        if low < 0 or high <= 0:
            raise ValueError("range 必须为非负数，且 high > 0")
        if low >= high:
            raise ValueError(f"range 下限必须小于上限：{low} >= {high}")
        if high > 1.0:
            raise ValueError(f"range 上限不应超过 1.0（相对幅度）：{high}")
        return self


class Card(BaseModel):
    """一张策略卡。字段顺序即 YAML 书写顺序。"""

    model_config = {"extra": "forbid"}

    card_id: str = Field(pattern=r"^PB-R(10|[1-9])-\d{2}$")
    title: str = Field(min_length=4, max_length=40)
    root_causes: list[str] = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)
    risk: int = Field(ge=0, le=2)
    scope: list[Literal["sku", "style", "channel", "store"]] = Field(min_length=1)
    action_type: Literal["adjust_price", "adjust_bid", "swap_creative", "issue_coupon", "restock_alert"]
    preconditions: list[str] = Field(min_length=1)
    steps: list[str] = Field(min_length=2)
    expected_effect: ExpectedEffect
    success_metric: str
    source: str = ""
    version: int = Field(default=1, ge=1)

    @field_validator("root_causes")
    @classmethod
    def _check_roots(cls, value: list[str]) -> list[str]:
        unknown = [code for code in value if code not in ROOT_CAUSES]
        if unknown:
            raise ValueError(f"未知根因码：{unknown}（只允许 {sorted(ROOT_CAUSES)}）")
        return list(dict.fromkeys(value))

    @field_validator("scope")
    @classmethod
    def _dedup_scope(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _check_success_metric(self) -> "Card":
        if self.success_metric not in self.metrics and self.success_metric != self.expected_effect.metric:
            raise ValueError(
                f"success_metric={self.success_metric} 既不在 metrics 里，也不是 expected_effect.metric"
            )
        return self

    @model_validator(mode="after")
    def _check_prefix_root(self) -> "Card":
        """card_id 里的根因码必须出现在 root_causes 里，避免前端按 code 分组时错位。"""
        prefix = self.card_id.split("-")[1]
        if prefix not in self.root_causes:
            raise ValueError(
                f"card_id 前缀 {prefix} 必须出现在 root_causes 里（当前 {self.root_causes}）"
            )
        return self

    # ---- 派生 ----

    @property
    def root_names(self) -> list[str]:
        return [ROOT_CAUSES[code] for code in self.root_causes]

    def search_text(self) -> str:
        """进 FTS / 向量索引的正文：把结构化字段摊成自然语言，便于中文检索。"""
        lines = [
            f"{self.card_id} {self.title}",
            "适用根因：" + "、".join(f"{code} {ROOT_CAUSES[code]}" for code in self.root_causes),
            "影响指标：" + "、".join(self.metrics),
            "触发条件：" + "；".join(self.preconditions),
            "执行步骤：" + "；".join(self.steps),
            f"预期：{self.expected_effect.metric} 在 {self.expected_effect.window_days} 天内"
            f"{'下降' if self.expected_effect.direction == 'down' else '上升'} "
            f"{self.expected_effect.range[0]:.0%}~{self.expected_effect.range[1]:.0%}",
            f"动作类型：{self.action_type}；风险等级：{self.risk}",
        ]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        data = self.model_dump()
        data["root_names"] = self.root_names
        return data
