"""
智能标注会话与文件状态存储。
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.config import settings
from app.services.annotation_constants import (
    BACKEND_ROOT,
    IMAGE_EXTENSIONS,
    PROJECT_ROOT,
    VIDEO_EXTENSIONS,
)


ANNOTATION_ROOT = (BACKEND_ROOT / settings.ANNOTATION_STORAGE_DIR).resolve()
SESSION_ROOT = ANNOTATION_ROOT / "sessions"
ARTIFACT_ROOT = ANNOTATION_ROOT / "artifacts"
EXPORT_ROOT = ANNOTATION_ROOT / "exports"


def ensure_annotation_dirs() -> None:
    for path in (ANNOTATION_ROOT, SESSION_ROOT, ARTIFACT_ROOT, EXPORT_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def normalize_input_path(raw_path: str, *, allow_missing: bool = True) -> str:
    text = (raw_path or "").strip()
    if not text:
        return ""

    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve(strict=False))

    candidate_roots = [PROJECT_ROOT]
    resolved_candidates = [(root / candidate).resolve(strict=False) for root in candidate_roots]

    for resolved in resolved_candidates:
        if resolved.exists():
            return str(resolved)

    if allow_missing:
        return str(resolved_candidates[0])
    raise FileNotFoundError(text)


def build_session_id(workspace_id: int, media_type: str, source_dir: str) -> str:
    normalized = normalize_input_path(source_dir)
    digest = hashlib.sha1(f"{workspace_id}:{media_type}:{normalized}".encode("utf-8")).hexdigest()
    return f"{media_type}_{workspace_id}_{digest[:20]}"


def build_item_id(file_path: str) -> str:
    return hashlib.sha1(str(Path(file_path).resolve(strict=False)).encode("utf-8")).hexdigest()[:16]


def get_session_file(session_id: str) -> Path:
    ensure_annotation_dirs()
    return SESSION_ROOT / f"{session_id}.json"


def get_session_artifact_dir(session_id: str) -> Path:
    ensure_annotation_dirs()
    path = ARTIFACT_ROOT / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_session(session_id: str) -> Optional[dict[str, Any]]:
    session_file = get_session_file(session_id)
    if not session_file.exists():
        return None
    with open(session_file, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def load_session_for_source(workspace_id: int, media_type: str, source_dir: str) -> Optional[dict[str, Any]]:
    return load_session(build_session_id(workspace_id, media_type, source_dir))


def save_session(session: dict[str, Any]) -> dict[str, Any]:
    ensure_annotation_dirs()
    session_copy = deepcopy(session)
    session_copy["updated_at"] = utc_now_iso()
    session_file = get_session_file(session_copy["id"])
    with open(session_file, "w", encoding="utf-8") as file_obj:
        json.dump(session_copy, file_obj, ensure_ascii=False, indent=2)
    return session_copy


def scan_source_files(source_dir: str, media_type: str) -> list[str]:
    base_dir = Path(normalize_input_path(source_dir, allow_missing=False))
    if not base_dir.exists() or not base_dir.is_dir():
        raise FileNotFoundError(str(base_dir))

    suffixes = VIDEO_EXTENSIONS if media_type == "video" else IMAGE_EXTENSIONS
    files = [
        str(path.resolve())
        for path in sorted(base_dir.iterdir())
        if path.is_file() and path.suffix.lower() in suffixes
    ]
    return files


def iter_unfinished_sessions() -> Iterable[str]:
    ensure_annotation_dirs()
    for session_file in sorted(SESSION_ROOT.glob("*.json")):
        try:
            with open(session_file, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
        except Exception:
            continue
        if payload.get("status") in {"pending", "processing"}:
            yield str(payload.get("id"))


def build_item_artifact_paths(session_id: str, item_id: str, stem: str) -> dict[str, str]:
    artifact_dir = get_session_artifact_dir(session_id) / item_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return {
        "preview_image_path": str((artifact_dir / f"{stem}_preview.jpg").resolve()),
        "preview_result_path": str((artifact_dir / f"{stem}_result.json").resolve()),
        "preview_video_path": str((artifact_dir / f"{stem}_tracked.mp4").resolve()),
        "tracking_json_path": str((artifact_dir / f"{stem}_tracks.json").resolve()),
        "description_path": str((artifact_dir / f"{stem}_desc.txt").resolve()),
    }


def sanitize_session(session: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(session)
    for item in data.get("items", []):
        item.pop("source_path", None)
        item.pop("artifact_dir", None)
        item.pop("preview_image_path", None)
        item.pop("preview_result_path", None)
        item.pop("preview_video_path", None)
        item.pop("tracking_json_path", None)
        item.pop("description_path", None)
    return data
