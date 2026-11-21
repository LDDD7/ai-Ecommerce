"""一键启动后端（跨平台，不依赖 PowerShell 执行策略）。

用法（在 project/ 下）：

    python -m tools.dev                 # 默认 8000
    python -m tools.dev --port 8010     # 换端口
    python -m tools.dev --reload        # 开发热重载（走 uvicorn 子进程）
    python -m tools.dev --python "E:\\llm\\third\\test\\k12\\.venv\\Scripts\\python.exe"

前端另开一个终端：cd web; npm run dev  （Vite 已把 /api 代理到 8000）

说明：在 Windows 上如果 8000 端口被别的进程半占用（`netstat` 看不到、uvicorn 直接
报 `[Errno 10048]`），本脚本会先用 `SO_REUSEADDR` 自己绑定端口，再把 socket 交给
uvicorn——这样不用改端口、也不用重启系统服务。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _check_files() -> None:
    if not (PROJECT / ".env").exists():
        print("[warn] 没有 .env：将按默认配置启动（无 key 时自动走规则版 + mock 回放，功能不缺）")
    if not (PROJECT / "data" / "store.db").exists():
        print("[warn] 没有 data/store.db：先跑 python -m app.data.seed")
    if not (PROJECT / "web" / "node_modules").exists():
        print("[info] 前端依赖未安装：cd web; npm install")


def _serve_inprocess(host: str, port: int) -> int:
    """预绑定 socket 再交给 uvicorn，规避 Windows 的 10048。"""
    sys.path.insert(0, str(PROJECT))

    import uvicorn

    from app.main import app as fastapi_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        print(f"[error] 绑定 {host}:{port} 失败：{exc}")
        print("        换端口试试：python -m tools.dev --port 8010")
        return 2
    sock.listen(2048)
    sock.set_inheritable(True)

    config = uvicorn.Config(fastapi_app, host=host, port=port, log_level="info")
    uvicorn.Server(config).run(sockets=[sock])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 StoreDoctor 后端")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--python", default=sys.executable, help="解释器路径（默认当前解释器）")
    parser.add_argument("--skip-checks", action="store_true")
    args = parser.parse_args(argv)

    if not args.skip_checks:
        _check_files()

    base = f"http://{args.host}:{args.port}"
    print()
    print(f"后端  : {base}/api/health")
    print(f"前端  : 另开终端 -> {PROJECT / 'web'}; npm run dev  (http://localhost:5173)")
    print()

    if args.reload:
        cmd = [
            args.python,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--reload",
        ]
        return subprocess.call(cmd, cwd=str(PROJECT))

    return _serve_inprocess(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
