"""给策略库/规则库建检索索引：FTS5（必须）+ 向量（可选）。

用法：`python -m tools.index_vectors [--force]`
无 torch/无模型时只建 FTS，并提示 `EMBED_MODE=off`，检索会自动降级。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.core.config import get_settings  # noqa: E402
from app.retrieval import embed, fts, load_docs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建检索索引（FTS5 + 可选向量）")
    parser.add_argument("--db", default=None, help="数据库路径，默认取 settings.db_path")
    parser.add_argument("--force", action="store_true", help="重算全部向量")
    parser.add_argument("--no-vec", action="store_true", help="只建 FTS")
    args = parser.parse_args(argv)

    settings = get_settings()
    db_path = Path(args.db or settings.db_path)
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        return 1

    docs = load_docs(db_path)
    if not docs:
        print("没有可索引的语料（策略卡与平台规则都为空）")
        return 1

    fts.ensure(db_path)
    written = fts.index(db_path, docs)
    print(f"FTS 索引：{written} 条（共 {fts.count(db_path)} 行）")

    if args.no_vec:
        print("跳过向量（--no-vec）")
        return 0

    result = embed.ensure_index(db_path, docs, force=args.force)
    if result.get("reason"):
        print(f"向量：不可用（{result['reason']}）→ 检索将退化为 FTS 单路")
    else:
        print(f"向量：模型 {result['model']}，新算 {result['embedded']} 条，跳过 {result['skipped']} 条，维度 {result['dim']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
