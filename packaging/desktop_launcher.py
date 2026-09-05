"""PyInstaller desktop entry: start the local service and open a browser."""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


SERVICE_ID = "meridian-analytics-workbench"


def user_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "Meridian Analytics Workbench"


def available_port(preferred: int = 5001) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise RuntimeError("无法分配本地服务端口")


def wait_until_ready(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # ``url`` is constructed by main() from a fixed loopback host and an integer port.
            with urllib.request.urlopen(f"{url}/api/health", timeout=1.0) as response:  # noqa: S310
                payload = json.loads(response.read(4096))
            if response.status == 200 and payload.get("ok") and payload.get("service") == SERVICE_ID:
                return True
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.15)
    return False


def open_when_ready(url: str) -> None:
    if wait_until_ready(url) and os.getenv("MERIDIAN_NO_BROWSER", "0") != "1":
        webbrowser.open(url, new=1, autoraise=True)


def main() -> int:
    multiprocessing.freeze_support()
    storage = Path(os.getenv("MERIDIAN_STORAGE_DIR") or user_data_dir()).expanduser().resolve()
    storage.mkdir(parents=True, exist_ok=True)
    os.environ["MERIDIAN_STORAGE_DIR"] = str(storage)
    os.environ.setdefault("MERIDIAN_ENV", "development")
    os.environ.setdefault("MERIDIAN_DISABLE_SCHEDULER", "0")
    host = "127.0.0.1"
    port = available_port(int(os.getenv("MERIDIAN_PORT", "5001")))
    url = f"http://{host}:{port}"
    from backend import create_app
    from waitress import serve

    app = create_app()
    threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    serve(app, host=host, port=port, threads=12, channel_timeout=300)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
