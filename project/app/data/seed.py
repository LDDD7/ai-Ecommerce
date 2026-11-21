"""数据生成 CLI 入口。

    python -m app.data.seed --tiny
    python -m app.data.seed --sku-count 300 --days 180 --orders 200000 --seed 42

真正的实现在 `app.data.generator`，这里只做程序化调用与「缺库自动建」的薄封装。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.data import generator


def ensure_db(db_path: Path | None = None, **kwargs) -> Path:
    """库不存在就生成一份；返回路径。测试与本地开发用，生产不调用。"""
    path = Path(db_path) if db_path else generator.BASE_DIR / "data" / "store.db"
    if path.exists():
        return path
    kwargs.setdefault("sku_count", 300)
    kwargs.setdefault("days", 180)
    kwargs.setdefault("orders_target", 200_000)
    kwargs.setdefault("seed", 42)
    kwargs.setdefault("end", generator.date(2026, 11, 18))
    generator.build(path, **kwargs)
    return path


def main(argv: list[str] | None = None) -> int:
    return generator.main(argv)


if __name__ == "__main__":
    sys.exit(main())
