"""
媒体接入与处理的公共工具。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from fastapi import UploadFile

from app.core.config import settings
from app.models.models import MediaResourceType

BACKEND_DIR = Path(__file__).resolve().parents[2]
MEDIA_ROOT = (BACKEND_DIR / settings.MEDIA_STORAGE_DIR).resolve()
MEDIA_UPLOAD_ROOT = MEDIA_ROOT / "uploads"
MEDIA_DERIVED_ROOT = MEDIA_ROOT / "derived"
MEDIA_KEYFRAME_ROOT = MEDIA_DERIVED_ROOT / "keyframes"
QUERY_UPLOAD_ROOT = (BACKEND_DIR / settings.MEDIA_QUERY_UPLOAD_DIR).resolve()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
CSV_EXTENSIONS = {".csv"}


@dataclass
class ResolvedMediaInput:
    path: str
    resource_type: MediaResourceType
    source_type: str
    file_name: str
    mime_type: Optional[str]
    file_size: Optional[int]
    checksum: Optional[str]
    dedupe_key: str


def ensure_media_dirs() -> None:
    for path in (MEDIA_ROOT, MEDIA_UPLOAD_ROOT, MEDIA_DERIVED_ROOT, MEDIA_KEYFRAME_ROOT, QUERY_UPLOAD_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def normalize_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def guess_media_type(path: str) -> Optional[MediaResourceType]:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return MediaResourceType.IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return MediaResourceType.VIDEO
    return None


def is_csv_path(path: str) -> bool:
    return Path(path).suffix.lower() in CSV_EXTENSIONS


def compute_checksum(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_dedupe_key(*parts: Optional[str]) -> str:
    normalized = "||".join(str(part or "") for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def save_upload_file(
    upload: UploadFile,
    *,
    sub_dir: str,
) -> tuple[str, int, Optional[str], str]:
    ensure_media_dirs()
    target_dir = MEDIA_UPLOAD_ROOT / sub_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    original_name = upload.filename or "upload.bin"
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(original_name).name}"
    target_path = target_dir / safe_name

    content = await upload.read()
    with open(target_path, "wb") as file_obj:
        file_obj.write(content)

    mime_type = upload.content_type or mimetypes.guess_type(original_name)[0]
    checksum = hashlib.sha256(content).hexdigest()
    return str(target_path), len(content), mime_type, checksum


def copy_local_file(source_path: str, *, sub_dir: str) -> tuple[str, int, Optional[str], str]:
    ensure_media_dirs()
    src = Path(source_path)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(source_path)

    target_dir = MEDIA_UPLOAD_ROOT / sub_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{uuid.uuid4().hex[:8]}_{src.name}"
    shutil.copy2(src, target_path)
    file_size = target_path.stat().st_size
    mime_type = mimetypes.guess_type(target_path.name)[0]
    checksum = compute_checksum(str(target_path))
    return str(target_path), file_size, mime_type, checksum


def iter_supported_files(input_paths: Iterable[str]) -> list[ResolvedMediaInput]:
    resolved: list[ResolvedMediaInput] = []
    seen: set[str] = set()

    for raw_path in input_paths:
        if not raw_path:
            continue
        normalized = normalize_path(raw_path)
        path_obj = Path(normalized)
        if not path_obj.exists():
            raise FileNotFoundError(normalized)

        if path_obj.is_dir():
            iterator = sorted(
                file_path
                for file_path in path_obj.rglob("*")
                if file_path.is_file() and guess_media_type(str(file_path))
            )
        else:
            iterator = [path_obj]

        for file_path in iterator:
            normalized_file = str(file_path.resolve())
            if normalized_file in seen:
                continue
            resource_type = guess_media_type(normalized_file)
            if not resource_type:
                continue
            seen.add(normalized_file)
            mime_type = mimetypes.guess_type(file_path.name)[0]
            file_size = file_path.stat().st_size
            checksum = compute_checksum(normalized_file)
            resolved.append(
                ResolvedMediaInput(
                    path=normalized_file,
                    resource_type=resource_type,
                    source_type="path",
                    file_name=file_path.name,
                    mime_type=mime_type,
                    file_size=file_size,
                    checksum=checksum,
                    dedupe_key=build_dedupe_key(normalized_file, checksum, resource_type.value),
                )
            )
    return resolved


def resolve_preview_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        if resolved.is_relative_to(MEDIA_ROOT):
            relative = resolved.relative_to(MEDIA_ROOT)
            return f"/media-files/{relative.as_posix()}"
        if resolved.is_relative_to(QUERY_UPLOAD_ROOT):
            relative = resolved.relative_to(QUERY_UPLOAD_ROOT)
            return f"/query-files/{relative.as_posix()}"
        relative = resolved.relative_to(BACKEND_DIR)
        return f"/{relative.as_posix()}"
    except Exception:
        return path
