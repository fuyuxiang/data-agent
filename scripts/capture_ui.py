"""Capture real browser screenshots of the product at several viewports.

This is a local verification helper for the V2 redesign: it boots the real
Flask app against a disposable SQLite database, seeds the demo data set and
walks the user-facing routes, writing PNG files for visual inspection.

Usage::

    python3 scripts/capture_ui.py --out .tmp/ui --routes workbench metrics skills
    python3 scripts/capture_ui.py --out .tmp/ui --widths 1280 1440 1920
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/ready", timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:  # pragma: no cover - startup race
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"服务在 {timeout} 秒内未就绪: {last}")


def build_app(database_path: Path, storage_dir: Path):
    os.environ["MERIDIAN_ENV"] = "development"
    os.environ["MERIDIAN_STORAGE_DIR"] = str(storage_dir)
    os.environ["MERIDIAN_ALLOW_SELF_REGISTRATION"] = "1"
    # No portal admin is created on purpose: a workspace with no users runs in
    # local mode, which is exactly the "first open" experience we want to see.
    os.environ.pop("MERIDIAN_PORTAL_AUTO_ADMIN", None)
    os.environ.pop("MERIDIAN_COOKIE_SECURE", None)
    os.environ.pop("MERIDIAN_TRUSTED_HOSTS", None)
    os.environ.pop("MERIDIAN_ALLOWED_ORIGINS", None)

    from backend import create_app

    return create_app({"TESTING": False, "DATABASE_PATH": str(database_path)})


def main() -> int:
    parser = argparse.ArgumentParser(description="启动真实应用并抓取 UI 截图")
    parser.add_argument("--out", default=".tmp/ui", help="截图输出目录")
    parser.add_argument("--widths", type=int, nargs="+", default=[1440])
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--routes", nargs="*", default=None, help="要访问的 hash 路由")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--keep-storage", action="store_true")
    parser.add_argument("--theme", default="light", choices=["light", "dark"])
    args = parser.parse_args()

    workdir = ROOT / ".tmp" / "ui-capture"
    workdir.mkdir(parents=True, exist_ok=True)
    database_path = workdir / "capture.sqlite3"
    storage_dir = workdir / "storage"
    for stale in (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")):
        stale.unlink(missing_ok=True)

    app = build_app(database_path, storage_dir)
    port = args.port or _free_port()
    base = f"http://127.0.0.1:{port}"

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_ready(base)
    print(f"应用已就绪: {base}")

    # Seed through the real HTTP surface so identity and authorization match
    # what a browser session would get.
    seed_request = urllib.request.Request(  # noqa: S310
        f"{base}/api/demo/seed",
        data=json.dumps({"workspace_id": "default"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(seed_request, timeout=60) as response:  # noqa: S310
            seeded = json.loads(response.read() or b"{}")
        print(f"演示数据已载入: {seeded.get('source', {}).get('name', '')}")
    except urllib.error.HTTPError as exc:
        print(f"演示数据载入失败 ({exc.code}): {exc.read()[:400]!r}")

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    routes = args.routes or [
        "workbench", "agents", "library", "metrics",
        "admin/agents", "admin/skills", "admin/data", "admin/metrics", "admin/knowledge",
        "admin/models", "admin/mcp", "admin/integrations", "admin/runs",
        "admin/evaluations", "admin/users", "admin/settings",
    ]
    written: list[str] = []
    with sync_playwright() as play:
        browser = play.chromium.launch()
        for width in args.widths:
            context = browser.new_context(
                viewport={"width": width, "height": args.height},
                device_scale_factor=1,
                locale="zh-CN",
            )
            page = context.new_page()
            if args.theme == "dark":
                page.add_init_script("localStorage.setItem('shuqing-theme', 'dark')")
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
                    if msg.type == "error" else None)
            for route in routes:
                page.goto(f"{base}/#{route}", wait_until="networkidle")
                page.wait_for_timeout(900)
                suffix = "" if args.theme == "light" else f"-{args.theme}"
                target = out_dir / f"{route.strip('/').replace('/', '-') or 'root'}-{width}{suffix}.png"
                page.screenshot(path=str(target), full_page=True)
                written.append(str(target))
                print(f"已保存 {target}")
            if errors:
                print(f"[{width}] 页面错误:")
                for item in dict.fromkeys(errors):
                    print(f"  - {item}")
            context.close()
        browser.close()

    server.shutdown()
    print(json.dumps({"screenshots": written}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
