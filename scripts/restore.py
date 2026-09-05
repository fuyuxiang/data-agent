#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:
    from .backup import BACKUP_MAGIC, decrypt_backup
except ImportError:  # Direct CLI execution: python scripts/restore.py
    from backup import BACKUP_MAGIC, decrypt_backup


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _validate_archive(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if not members:
        raise ValueError("备份归档为空")
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "storage":
            raise ValueError(f"备份包含越界路径：{member.name}")
        if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
            raise ValueError(f"备份包含不安全成员：{member.name}")
    if not any(member.name == "storage/meridian.sqlite3" for member in members):
        raise ValueError("备份缺少 storage/meridian.sqlite3")
    return members


def restore_backup(
    source: Path, destination: Path, *, encryption_key: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"备份不存在：{source}")
    if expected_sha256 and _sha256(source) != expected_sha256.lower().strip():
        raise ValueError("备份 SHA-256 校验失败")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("恢复目标必须是空目录")

    with tempfile.TemporaryDirectory(prefix=".meridian-restore-", dir=destination.parent) as temp_dir:
        archive_path = source
        with source.open("rb") as stream:
            encrypted = stream.read(len(BACKUP_MAGIC)) == BACKUP_MAGIC
        if encrypted:
            if not encryption_key:
                raise ValueError("加密备份需要 MERIDIAN_BACKUP_KEY")
            archive_path = Path(temp_dir) / "backup.tar.gz"
            decrypt_backup(source, archive_path, encryption_key)
        extract_root = Path(temp_dir) / "verified"
        extract_root.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            members = _validate_archive(archive)
            archive.extractall(extract_root, members=members, filter="data")

        database = extract_root / "storage" / "meridian.sqlite3"
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ValueError(f"恢复后的数据库完整性校验失败：{integrity}")
        os.replace(extract_root / "storage", destination / "storage")
    return {
        "source": str(source), "destination": str(destination), "sha256": _sha256(source),
        "encrypted": encrypted, "database_integrity": integrity,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and restore a Meridian storage backup")
    parser.add_argument("backup")
    parser.add_argument("--destination", required=True, help="必须是空目录")
    parser.add_argument("--sha256", help="可选的预期 SHA-256")
    args = parser.parse_args()
    result = restore_backup(
        Path(args.backup), Path(args.destination),
        encryption_key=os.getenv("MERIDIAN_BACKUP_KEY", "").strip() or None,
        expected_sha256=args.sha256,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
