"""诊断域：异常检测 → 分解 → 贡献 → 漏斗 → 证据 → 诊断卡。

本包只做「算」，不碰 LLM、不碰 HTTP；上层（orchestrator/api）负责编排。
"""

from app.diagnose.anomaly import AnomalyPoint, DetectionResult, Window, detect, summarize
from app.diagnose.contribution import (
    ContributionResult,
    Part,
    additive_contribution,
    contribution,
    describe as describe_contribution,
    ratio_contribution,
)
from app.diagnose.decompose import (
    Decomposition,
    Factor,
    decompose_orders,
    decompose_product,
    describe as describe_decomposition,
    gmv_decompose,
)
from app.diagnose.diagnosis import DiagnosisCard, build_diagnosis
from app.diagnose.evidence import Evidence, complete_only, is_complete
from app.diagnose.funnel import Funnel, Step, describe as describe_funnel, funnel, funnel_by_dim
from app.diagnose.hypothesis import Candidate, codes_in, explain
from app.diagnose.recheck import ScopeRecheck, recheck_many, recheck_scope
from app.diagnose.replan import Recovery, plan_recovery

__all__ = [
    "AnomalyPoint",
    "Candidate",
    "ContributionResult",
    "Decomposition",
    "DetectionResult",
    "DiagnosisCard",
    "Evidence",
    "Factor",
    "Funnel",
    "Part",
    "Recovery",
    "ScopeRecheck",
    "Step",
    "Window",
    "additive_contribution",
    "build_diagnosis",
    "codes_in",
    "complete_only",
    "contribution",
    "decompose_orders",
    "decompose_product",
    "describe_contribution",
    "describe_decomposition",
    "describe_funnel",
    "detect",
    "explain",
    "funnel",
    "funnel_by_dim",
    "gmv_decompose",
    "is_complete",
    "plan_recovery",
    "ratio_contribution",
    "recheck_many",
    "recheck_scope",
    "summarize",
]
