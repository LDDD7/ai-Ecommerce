"""创意工单的数据契约（spec §25）。

一句话：**把素材诊断的结论（`NO_SIZE_CHART` / `TEXT_HEAVY` …）变成一张可审批的改进工单**。

设计上有三条硬约束：

1. **不训练模型、不生视频**（spec §2.2）。这里只做「改图」——本地 PIL 重排文字、补尺码表、
   加场景底、去水印；有条件时交给百炼的图生图模型重绘。
2. **工单必须可复核**。每张工单都带 `before`（改之前的图与属性）与 `after`（改之后的图与实测属性），
   外加 `compliance` 检查项，避免「模型说改好了」但实际上文字更满。
3. **落回执行闭环**。工单本身不生效；`POST /api/creative/{id}/apply` 会起草一条 `swap_creative`
   动作，走既有的审批 → 执行 → 回滚链路。这样「生成素材」和「换素材」是两件事，责任分得清。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.vision.schema import ISSUE_SPECS

# 本地规则改图 / 百炼图生图。第三档 `mock` 只给测试用来验证「不调外部服务也能跑通链路」。
PROVIDERS = ("local", "dashscope", "mock")

TARGETS = ("main_image", "detail_image")

# 能自动改的问题码；拼图/无模特需要重拍，只能转人工。
AUTO_FIXABLE = ("TEXT_HEAVY", "NO_SIZE_CHART", "PLAIN_WHITE", "WATERMARK")
MANUAL_ONLY = ("COLLAGE", "NO_MODEL")

MAX_ISSUES = 6


class CreativeConstraints(BaseModel):
    """改图的边界。默认值即「能投首图」的常见要求。"""

    model_config = ConfigDict(extra="forbid")

    max_text_coverage: float = Field(default=0.28, ge=0.0, le=0.6)
    add_size_chart: bool = True
    keep_price_tag: bool = False
    scene: Literal["auto", "indoor", "outdoor", "plain"] = "auto"
    size: int = Field(default=800, ge=400, le=1600)


class CreativeRequest(BaseModel):
    """调用方的入参。`issues` 通常来自素材诊断，也可以手工传（演示时有用）。"""

    model_config = ConfigDict(extra="forbid")

    sku_id: str = Field(pattern=r"^SKU\d{4}$")
    issues: list[str] = Field(default_factory=list, max_length=MAX_ISSUES)
    target: Literal["main_image", "detail_image"] = "main_image"
    provider: str | None = None
    constraints: CreativeConstraints = Field(default_factory=CreativeConstraints)
    note: str = Field(default="", max_length=200)

    @field_validator("issues")
    @classmethod
    def _check_issues(cls, value: list[str]) -> list[str]:
        unknown = [code for code in value if code not in ISSUE_SPECS]
        if unknown:
            raise ValueError(f"未知的素材问题码：{', '.join(unknown)}")
        # 去重但保序：同一张工单里同一个问题只改一次
        return list(dict.fromkeys(value))


class CreativeAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    kind: Literal["image", "copy"] = "image"
    path: str = ""
    url: str = ""
    width: int = 0
    height: int = 0
    format: str = "png"


class CreativeCopy(BaseModel):
    """文案也是素材的一部分：图片里的文字改了，标题与卖点要跟着改，否则前后不一致。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    selling_points: list[str] = Field(default_factory=list)
    description: str = ""
    size_text: str = ""


class ComplianceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class Compliance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    items: list[ComplianceItem] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class CreativeFix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue: str
    action: str
    detail: str
    status: Literal["done", "manual"] = "done"


class CreativeResult(BaseModel):
    """工单的完整结果。前端只认这几个字段，改结构要同步 `web/src/api.ts`。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    sku_id: str
    provider: str
    status: Literal["ready", "degraded"] = "ready"
    target: str = "main_image"
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    assets: list[CreativeAsset] = Field(default_factory=list)
    copywriting: CreativeCopy
    compliance: Compliance = Field(default_factory=Compliance)
    fixes: list[CreativeFix] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    note: str = ""


class CreativeError(RuntimeError):
    """工单层可预期的失败（找不到 SKU / 找不到图 / 配图失败）。"""


def resolve_provider(requested: str | None, *, key_ready: bool) -> str:
    """决定用哪一档：

    - 显式传 `local` / `mock` / `dashscope` 就照办（`dashscope` 没 key 时降级，见 service）。
    - 没传：有百炼 key 且设置页选了 `dashscope` → dashscope；否则 local。
    """
    if requested:
        if requested not in PROVIDERS:
            raise ValueError(f"未知的 provider：{requested}（可选：{', '.join(PROVIDERS)}）")
        return requested
    return "dashscope" if key_ready else "local"


def split_issues(issues: list[str]) -> tuple[list[str], list[str]]:
    """拆成「能自动改」与「只能转人工」。"""
    auto = [code for code in issues if code in AUTO_FIXABLE]
    manual = [code for code in issues if code in MANUAL_ONLY]
    return auto, manual
