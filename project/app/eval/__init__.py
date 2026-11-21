"""评测体系（T7）。

四个套件 + 消融 + 门禁：

- ``suites.query``    问数：与语义层无关的独立 SQL 对账（20 条）
- ``suites.diagnose`` 诊断：从 ``anomaly_gt`` 抽 25 个场景，看根因 top-1/top-3
- ``suites.action``   动作：15 条（10 条正常起草 + 5 条护栏拒绝）
- ``suites.vision``   素材：10 条 field-level 准确率

``runner`` 负责跑套件并出报告，``gate`` 负责回归门禁阈值，
``ablation`` 负责检索四档与管线开关的消融。
"""

from app.eval.types import Case, CaseResult, SuiteResult

__all__ = ["Case", "CaseResult", "SuiteResult"]
