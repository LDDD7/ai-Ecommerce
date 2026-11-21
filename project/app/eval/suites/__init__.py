"""四个评测套件。

每个模块对外暴露同一组东西：

- ``SUITE``            套件名（"query" / "diagnose" / "action" / "vision"）
- ``cases(db_path)``   生成用例（确定性；允许只读数据库取日期/样本）
- ``run(db_path, *, limit=None) -> SuiteResult``
"""

SUITES = ("query", "diagnose", "action", "vision")
