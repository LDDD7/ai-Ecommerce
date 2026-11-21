"""查数（QuerySpec 编译器 + 自然语言解析）包。"""

from app.query.nl import NLResult, parse_question

__all__ = ["NLResult", "parse_question"]
