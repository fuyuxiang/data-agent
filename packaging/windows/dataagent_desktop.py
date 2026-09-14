from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


APP_NAME = "DataAgent"
DEFAULT_PORT = 5001


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2]


def _local_app_data() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if base:
        return Path(base)
    return Path.home() / "AppData" / "Local"


def _is_port_free(host: str, port: int) -> bool:
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "*"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((connect_host, port)) != 0


def _choose_port(host: str, requested: int) -> int:
    if _is_port_free(host, requested):
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _configure_environment(host: str, port: int, storage_dir: Path, frontend_dir: Path) -> None:
    os.environ.setdefault("MERIDIAN_ENV", "development")
    os.environ["MERIDIAN_HOST"] = host
    os.environ["MERIDIAN_PORT"] = str(port)
    os.environ["MERIDIAN_FRONTEND_DIR"] = str(frontend_dir)
    os.environ.setdefault("MERIDIAN_STORAGE_DIR", str(storage_dir))
    os.environ.setdefault("MERIDIAN_COOKIE_SECURE", "0")
    os.environ.setdefault(
        "MERIDIAN_ALLOWED_ORIGINS",
        f"http://{host}:{port},http://localhost:{port},http://127.0.0.1:{port}",
    )
    if host in {"0.0.0.0", "::", "*"}:
        os.environ.setdefault("MERIDIAN_TRUSTED_HOSTS", "")
    else:
        os.environ.setdefault("MERIDIAN_TRUSTED_HOSTS", f"{host},localhost,127.0.0.1")


def _open_browser_later(url: str) -> None:
    def open_when_ready() -> None:
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=open_when_ready, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="Start DataAgent desktop server")
    parser.add_argument("--host", default=os.getenv("MERIDIAN_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MERIDIAN_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--storage-dir", type=Path, default=None)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the default browser automatically")
    args = parser.parse_args()

    resources = _resource_root()
    frontend_dir = resources / "frontend" / "dist"
    if not (frontend_dir / "index.html").is_file():
        frontend_dir = resources / "frontend"
    if not (frontend_dir / "index.html").is_file():
        raise RuntimeError(f"前端资源不存在：{frontend_dir}")

    storage_dir = args.storage_dir or (_local_app_data() / APP_NAME / "storage")
    storage_dir.mkdir(parents=True, exist_ok=True)
    port = _choose_port(args.host, int(args.port))
    _configure_environment(args.host, port, storage_dir, frontend_dir)

    from backend import create_app
    from waitress import serve

    access_host = "127.0.0.1" if args.host in {"0.0.0.0", "::", "*"} else args.host
    url = f"http://{access_host}:{port}/"
    print(f"{APP_NAME} 正在启动：{url}")
    print(f"监听地址：{args.host}:{port}")
    print(f"数据目录：{storage_dir}")
    print("关闭此窗口即可停止本地服务。")
    if not args.no_browser:
        _open_browser_later(url)
    serve(create_app(), host=args.host, port=port, threads=12, channel_timeout=300)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
