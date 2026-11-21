"""素材属性的数据契约（spec §19.1）。

这张表的字段是「让模型看着图回答」的问题清单，不是给人看的描述：每个字段都能被判对错，
所以 vision suite 才能算准确率，而不是只贴一段「图片里有一个模特穿裙子」。

约定：
- `source` 只有三种取值：`mock`（合成图的真值回放）、`vlm`（模型现算）、`cache`（命中复核缓存）。
- `text_coverage` 是文字像素面积占比的估计值（0~1），判分时允许 ±0.10 的误差。
- `quality_flags` 由属性派生（`derive_flags`），不允许模型自由发挥。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 提示词版本：改提示词就加一版，缓存键里带这个值，避免新旧结果混用。
PROMPT_VERSION = "v1"

ROLES = ("main", "detail", "scene")

# 问题码 -> 人话 + 关联根因 + 建议动作。
# root_causes 用于把素材问题挂到诊断卡上；action 与策略库的五类动作同名。
ISSUE_SPECS: dict[str, dict[str, Any]] = {
    "NO_SIZE_CHART": {
        "label": "缺尺码表",
        "root_causes": ["R4"],
        "action": "swap_creative",
        "detail": "主图与详情页都没有尺码表，尺码咨询和「尺码偏小」类退货会偏高",
    },
    "TEXT_HEAVY": {
        "label": "文字占比偏高",
        "root_causes": ["R2"],
        "action": "swap_creative",
        "detail": "图上文字面积占比偏高，手机端缩略图里主体被文字挤掉，点击率通常要吃亏",
    },
    "PLAIN_WHITE": {
        "label": "纯白底无场景",
        "root_causes": ["R2"],
        "action": "swap_creative",
        "detail": "纯白底缺少穿着场景，在信息流里辨识度低，容易被划走",
    },
    "WATERMARK": {
        "label": "主图有水印",
        "root_causes": ["R2"],
        "action": "swap_creative",
        "detail": "主图带水印，多数平台判定为不合规素材，也影响观感",
    },
    "COLLAGE": {
        "label": "拼接图",
        "root_causes": ["R2"],
        "action": "swap_creative",
        "detail": "拼接图在首图位会被压缩，细节丢失、主体不明确",
    },
    "NO_MODEL": {
        "label": "无模特上身",
        "root_causes": ["R2"],
        "action": "swap_creative",
        "detail": "没有模特上身图，买家难以判断版型，转化率和退货率都会受影响",
    },
}

ISSUE_CODES = tuple(ISSUE_SPECS)

# 合成数据里的 `quality_flags` 是中文（生成器写的是人话），这里是唯一的翻译表：
# 回放真值、评测比对、前端展示都走它，避免同一个 flag 出现两种写法。
CN_TO_CODE: dict[str, str] = {
    "无尺码表": "NO_SIZE_CHART",
    "文字占比偏高": "TEXT_HEAVY",
    "纯白底无场景": "PLAIN_WHITE",
    "有水印": "WATERMARK",
    "拼接图": "COLLAGE",
    "无模特": "NO_MODEL",
    "无模特上身": "NO_MODEL",
}

CODE_TO_CN: dict[str, str] = {v: k for k, v in CN_TO_CODE.items()}


def normalize_flags(raw: list[str] | None) -> list[str]:
    """把中文/英文混着的 flag 统一成问题码，未知项原样保留（便于发现数据问题）。"""
    out: list[str] = []
    for item in raw or []:
        token = str(item).strip()
        if not token:
            continue
        code = CN_TO_CODE.get(token, token)
        if code not in out:
            out.append(code)
    return out


class MaterialIssue(BaseModel):
    """一条素材问题，已折算到根因与动作。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    detail: str
    root_causes: list[str] = Field(default_factory=list)
    action: str = ""
    severity: Literal["high", "medium", "low"] = "medium"

    @classmethod
    def from_code(cls, code: str, severity: str = "medium") -> MaterialIssue:
        spec = ISSUE_SPECS[code]
        return cls(
            code=code,
            label=spec["label"],
            detail=spec["detail"],
            root_causes=list(spec["root_causes"]),
            action=spec["action"],
            severity=severity,  # type: ignore[arg-type]
        )


def derive_flags(
    *,
    text_coverage: float,
    model_present: bool,
    scene: str,
    background: str,
    size_chart_present: bool,
    watermark: bool,
    collage: bool,
) -> list[str]:
    """由属性派生问题码。阈值与 spec §19.2 一致，集中在函数里方便评测对齐。"""
    flags: list[str] = []
    if not size_chart_present:
        flags.append("NO_SIZE_CHART")
    if text_coverage > 0.35:
        flags.append("TEXT_HEAVY")
    if scene in ("纯白底", "") and background in ("纯色", "纯白", ""):
        flags.append("PLAIN_WHITE")
    if watermark:
        flags.append("WATERMARK")
    if collage:
        flags.append("COLLAGE")
    if not model_present:
        flags.append("NO_MODEL")
    return flags


class MaterialFeatures(BaseModel):
    """一张图的属性。`source` 决定它是回放真值还是模型现算。"""

    model_config = ConfigDict(extra="forbid")

    sku_id: str
    role: Literal["main", "detail", "scene"] = "main"
    text_blocks: list[str] = Field(default_factory=list)
    text_coverage: float = 0.0
    model_present: bool = False
    scene: str = ""
    background: str = ""
    color_palette: list[str] = Field(default_factory=list)
    size_chart_present: bool = False
    watermark: bool = False
    collage: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    source: Literal["mock", "vlm", "cache"] = "mock"
    prompt_version: str = PROMPT_VERSION
    notes: str = ""

    @field_validator("text_coverage")
    @classmethod
    def _coverage_range(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    @field_validator("color_palette")
    @classmethod
    def _palette_hex(cls, v: list[str]) -> list[str]:
        out = []
        for item in v[:6]:
            s = str(item).strip()
            if s and not s.startswith("#"):
                s = "#" + s.lstrip("#")
            out.append(s.upper())
        return out

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def issues(self) -> list[MaterialIssue]:
        """把问题码折成带根因与动作的对象；高风险码给 high。"""
        high = {"NO_SIZE_CHART", "TEXT_HEAVY"}
        return [
            MaterialIssue.from_code(code, "high" if code in high else "medium")
            for code in self.quality_flags
            if code in ISSUE_SPECS
        ]

    def to_evidence(self) -> dict[str, Any]:
        """给报告层用的「图里看到什么」一句话证据（不是指标证据，指标证据另算）。"""
        bits = [f"{self.role} 图"]
        if self.text_coverage:
            bits.append(f"文字占比 {self.text_coverage * 100:.0f}%")
        bits.append("有模特" if self.model_present else "无模特")
        if self.scene:
            bits.append(f"场景：{self.scene}")
        bits.append("有尺码表" if self.size_chart_present else "无尺码表")
        if self.watermark:
            bits.append("有水印")
        if self.collage:
            bits.append("拼接图")
        return {
            "kind": "material",
            "sku_id": self.sku_id,
            "role": self.role,
            "text": "，".join(bits),
            "issues": [i.model_dump() for i in self.issues()],
            "source": self.source,
            "confidence": round(self.confidence, 3),
        }
