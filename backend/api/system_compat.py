from __future__ import annotations

import os
import platform
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, current_app, jsonify, request

from ..services.embeddings import (
    cloud_public_config,
    configure_cloud,
    download_local_model,
    info as embedding_info,
    local_installed,
    rebuild as rebuild_embeddings,
    save_mode,
)
from ..services.security import safe_http_request
from .common import api_errors, body, require_system_owner, workspace_id


bp = Blueprint("system_compat", __name__)
CURRENT_VERSION = "v1.0.0"
RELEASES_API = "https://api.github.com/repos/Zafer-Liu/Data-Analysis-Agent/releases/latest"
RELEASES_PAGE = "https://github.com/Zafer-Liu/Data-Analysis-Agent/releases/latest"
_directory_lock = threading.Lock()
_download_lock = threading.Lock()


def _version(value: str) -> tuple[int, int, int]:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(value or ""))
    return tuple(int(item) for item in match.groups()) if match else (0, 0, 0)


def _is_local_request() -> bool:
    remote = (request.remote_addr or "").split("%", 1)[0]
    if remote not in {"127.0.0.1", "::1"} or request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    origin = request.headers.get("Origin")
    return not origin or urlsplit(origin).netloc.lower() == request.host.lower()


@bp.post("/api/system/select-directory")
@api_errors
def select_directory():
    require_system_owner()
    if os.getenv("VERCEL") or not _is_local_request():
        return jsonify({"ok": False, "error": "原生目录选择仅允许从运行服务的本机页面调用。"}), 403
    if not _directory_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "目录选择窗口已打开。"}), 409
    try:
        initial = Path(str(body().get("initial_path") or Path.home())).expanduser()
        if not initial.is_dir():
            initial = Path.home()
        if platform.system() == "Darwin":
            script = 'POSIX path of (choose folder with prompt "选择要挂载的工作目录" default location POSIX file "' + str(initial).replace('"', '\\"') + '")'
            completed = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True,
                timeout=300, check=False,
            )
            if completed.returncode:
                if "-128" in completed.stderr:
                    return jsonify({"ok": True, "path": "", "cancelled": True})
                raise RuntimeError(completed.stderr.strip() or "osascript failed")
            selected = completed.stdout.strip()
        elif platform.system() == "Windows":
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                selected = filedialog.askdirectory(initialdir=str(initial), mustexist=True)
            finally:
                root.destroy()
        else:
            raise RuntimeError("当前平台不支持原生目录选择器，请手动输入绝对路径。")
        return jsonify({"ok": True, "path": str(Path(selected).resolve()) if selected else "", "cancelled": not bool(selected)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        _directory_lock.release()


@bp.get("/api/system/check-update")
def check_update():
    try:
        response = safe_http_request(
            "GET", RELEASES_API, timeout=15,
            headers={"User-Agent": f"Meridian/{CURRENT_VERSION}", "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        release = response.json()
        latest = str(release.get("tag_name") or "")
        return jsonify({
            "ok": True, "source": "github_api", "current_version": CURRENT_VERSION,
            "latest_version": latest, "has_update": _version(latest) > _version(CURRENT_VERSION),
            "release_url": release.get("html_url", RELEASES_PAGE),
            "release_notes": release.get("body", ""), "published_at": release.get("published_at", ""),
            "assets": [{
                "name": item.get("name", ""), "size": item.get("size", 0),
                "download_url": item.get("browser_download_url", ""),
            } for item in release.get("assets") or []],
        })
    except Exception as exc:
        return jsonify({
            "ok": False, "code": "github_update_check_failed", "error": f"检查更新失败：{exc}",
            "current_version": CURRENT_VERSION, "release_url": RELEASES_PAGE, "retryable": True,
        }), 502


@bp.post("/api/system/update")
def update_system():
    return jsonify({
        "ok": False,
        "error": "生产版本不允许通过 Web 请求覆盖应用文件，请使用经过签名验证的发布流水线部署。",
        "output": "在线自更新已永久禁用",
        "already_up_to_date": False,
        "updated": [], "added": [], "skipped": [],
    }), 410


@bp.get("/api/proxy-image")
def proxy_image():
    url = str(request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Invalid URL"}), 400
    try:
        response = safe_http_request(
            "GET", url, timeout=30, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Meridian-image-proxy/1.0)"},
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/") or content_type == "image/svg+xml":
            raise ValueError("远程内容不是可安全显示的位图")
        chunks, size = [], 0
        for chunk in response.iter_content(256 * 1024):
            size += len(chunk)
            if size > 10 * 1024 * 1024:
                raise ValueError("远程图片超过 10 MB")
            chunks.append(chunk)
        return Response(
            b"".join(chunks), mimetype=content_type,
            headers={"Cache-Control": "public, max-age=3600", "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Remote server error: {exc}"}), 502


@bp.get("/api/system/bge-model/status")
def bge_status():
    details = embedding_info(workspace_id())
    return jsonify({
        "ok": True, "installed": local_installed(), "neural_active": details["active"] in {"local", "cloud"},
        "init_error": "", "model_dir": str(current_app.config["SETTINGS"].storage_dir / "models" / "bge-small-zh-v1.5"),
    })


@bp.post("/api/system/bge-model/download")
@api_errors
def bge_download():
    require_system_owner()
    if not _download_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "Download already in progress."}), 409
    try:
        result = download_local_model()
        return jsonify({"ok": True, **result, "neural_active": embedding_info(workspace_id())["active"] == "local"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        _download_lock.release()


@bp.get("/api/system/embed-mode")
def embed_mode():
    return jsonify({"ok": True, **embedding_info(workspace_id(), probe=request.args.get("probe") == "true"), "installed": local_installed(), "neural_active": embedding_info(workspace_id())["active"] in {"local", "cloud"}, "init_error": ""})


@bp.post("/api/system/embed-mode")
@api_errors
def set_embed_mode():
    return jsonify({"ok": True, **save_mode(workspace_id(), str(body().get("mode") or ""))})


@bp.get("/api/system/embed-cloud-config")
def embed_cloud_config():
    return jsonify({"ok": True, **cloud_public_config(workspace_id())})


@bp.put("/api/system/embed-cloud-config")
@api_errors
def set_embed_cloud_config():
    payload = body()
    value = configure_cloud(
        workspace_id(), url=str(payload.get("url") or ""), model=str(payload.get("model") or ""),
        token=payload.get("token"), clear_token=bool(payload.get("clear_token")), verify=bool(payload.get("test")),
    )
    return jsonify({"ok": True, **value})


@bp.post("/api/system/embed-rebuild")
@api_errors
def embed_rebuild():
    return jsonify({"ok": True, **rebuild_embeddings(workspace_id())})


@bp.get("/api/instruction")
def instruction():
    root = current_app.config["SETTINGS"].root
    candidates = [root / "docs" / "USER_GUIDE.md", root / "README.md"]
    path = next((item for item in candidates if item.is_file()), None)
    if not path:
        return jsonify({"ok": False, "error": "Instruction document not found"}), 404
    return jsonify({"ok": True, "markdown": path.read_text(encoding="utf-8")})
