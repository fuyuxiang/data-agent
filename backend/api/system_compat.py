from __future__ import annotations

import os
import platform
import re
import subprocess
import tempfile
import threading
import zipfile
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
from .common import api_errors, body, workspace_id


bp = Blueprint("system_compat", __name__)
CURRENT_VERSION = "v1.0.0"
RELEASES_API = "https://api.github.com/repos/Zafer-Liu/Data-Analysis-Agent/releases/latest"
RELEASES_PAGE = "https://github.com/Zafer-Liu/Data-Analysis-Agent/releases/latest"
ARCHIVE_URL = "https://github.com/Zafer-Liu/Data-Analysis-Agent/archive/refs/heads/main.zip"
_directory_lock = threading.Lock()
_download_lock = threading.Lock()
_update_lock = threading.Lock()
_PROTECTED = {
    ".git", ".env", "storage", "uploads", "outputs", "data/datasource_config.json",
    "LLM/llm_config.json", "LLM/mcp_config.json", "LLM/embedding_config.json",
}


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
def select_directory():
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


def _protected(relative: Path) -> bool:
    if any(part in {"__pycache__", ".idea", ".vscode"} or part.endswith(".pyc") for part in relative.parts):
        return True
    return any(relative.parts[:len(Path(item).parts)] == Path(item).parts for item in _PROTECTED)


@bp.post("/api/system/update")
def update_system():
    if current_app.config.get("TESTING") or os.getenv("MERIDIAN_DISABLE_SELF_UPDATE", "0") == "1":
        return jsonify({
            "ok": False, "error": "当前运行模式已禁用自更新", "output": "当前运行模式已禁用自更新",
            "already_up_to_date": False, "updated": [], "added": [], "skipped": [],
        }), 409
    if not _update_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "更新正在进行"}), 409
    try:
        root = current_app.config["SETTINGS"].root.resolve()
        with tempfile.TemporaryDirectory(prefix="meridian-update-") as temporary:
            archive = Path(temporary) / "update.zip"
            response = safe_http_request("GET", ARCHIVE_URL, timeout=90, stream=True)
            response.raise_for_status()
            with archive.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    output.write(chunk)
            extracted = Path(temporary) / "extracted"
            with zipfile.ZipFile(archive) as value:
                for member in value.infolist():
                    target = (extracted / member.filename).resolve()
                    if extracted.resolve() not in target.parents and target != extracted.resolve():
                        raise ValueError("更新包包含越界路径")
                value.extractall(extracted)
            roots = [item for item in extracted.iterdir() if item.is_dir()]
            if len(roots) != 1:
                raise ValueError("更新包结构无效")
            updated, added, skipped = [], [], []
            for source in roots[0].rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(roots[0])
                if _protected(relative):
                    skipped.append(str(relative))
                    continue
                target = root / relative
                content = source.read_bytes()
                if target.is_file() and target.read_bytes() == content:
                    continue
                existed = target.exists()
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary_target = target.with_suffix(target.suffix + ".update")
                temporary_target.write_bytes(content)
                temporary_target.replace(target)
                (updated if existed else added).append(str(relative))
        already = not updated and not added
        output = "已是最新版本" if already else f"更新完成：{len(updated)} 个文件更新，{len(added)} 个文件新增"
        return jsonify({
            "ok": True, "output": output, "already_up_to_date": already,
            "updated": updated, "added": added, "skipped": skipped,
        })
    except Exception as exc:
        return jsonify({
            "ok": False, "error": str(exc), "output": str(exc), "already_up_to_date": False,
            "updated": [], "added": [], "skipped": [],
        }), 500
    finally:
        _update_lock.release()


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
def bge_download():
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
