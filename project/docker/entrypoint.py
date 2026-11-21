"""Docker 部署用的入口：库不存在就先生成，再起服务。

镜像里不带 82MB 数据库与 300 张素材（它们由生成器产出，属于数据不是代码）。
挂载了 data/ 卷就不用重复生成；没挂就会在容器内跑一次 seed（约 17s）。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

DB_PATH = pathlib.Path(os.environ.get("DB_PATH", "data/store.db"))
SEED_ENABLED = os.environ.get("SEED_ON_START", "1") not in ("0", "false", "False")
PORT = os.environ.get("PORT", "8000")


def main() -> int:
    if not DB_PATH.exists():
        if not SEED_ENABLED:
            print(f"[entrypoint] 数据库不存在且 SEED_ON_START=0：{DB_PATH}", file=sys.stderr)
            return 1
        print(f"[entrypoint] 数据库不存在，生成合成数据：{DB_PATH}", flush=True)
        subprocess.run([sys.executable, "-m", "app.data.seed", "--db", str(DB_PATH)], check=True)
        print("[entrypoint] 索引策略库与平台规则", flush=True)
        subprocess.run([sys.executable, "-m", "tools.index_vectors"], check=True)

    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", PORT,
    ]
    print("[entrypoint] " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
