#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


BACKUP_MAGIC = b"MERIDIAN-BACKUP\x01"
BACKUP_SALT_BYTES = 16
BACKUP_NONCE_BYTES = 12
BACKUP_TAG_BYTES = 16
BACKUP_KDF_ITERATIONS = 600_000
CHUNK_BYTES = 1024 * 1024


def _backup_key(secret: str, salt: bytes) -> bytes:
    if len(secret) < 32:
        raise ValueError("MERIDIAN_BACKUP_KEY 必须至少 32 个字符")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=BACKUP_KDF_ITERATIONS,
    ).derive(secret.encode("utf-8"))


def encrypt_backup(source: Path, output: Path, secret: str) -> None:
    salt = os.urandom(BACKUP_SALT_BYTES)
    nonce = os.urandom(BACKUP_NONCE_BYTES)
    header = BACKUP_MAGIC + salt + nonce
    encryptor = Cipher(algorithms.AES(_backup_key(secret, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as incoming, output.open("wb") as outgoing:
        outgoing.write(header)
        for chunk in iter(lambda: incoming.read(CHUNK_BYTES), b""):
            outgoing.write(encryptor.update(chunk))
        outgoing.write(encryptor.finalize())
        outgoing.write(encryptor.tag)
    output.chmod(0o600)


def decrypt_backup(source: Path, output: Path, secret: str) -> None:
    minimum = len(BACKUP_MAGIC) + BACKUP_SALT_BYTES + BACKUP_NONCE_BYTES + BACKUP_TAG_BYTES
    if source.stat().st_size < minimum:
        raise ValueError("备份文件格式无效")
    with source.open("rb") as incoming:
        magic = incoming.read(len(BACKUP_MAGIC))
        if magic != BACKUP_MAGIC:
            raise ValueError("不是 Meridian 加密备份")
        salt = incoming.read(BACKUP_SALT_BYTES)
        nonce = incoming.read(BACKUP_NONCE_BYTES)
        header = magic + salt + nonce
        incoming.seek(-BACKUP_TAG_BYTES, os.SEEK_END)
        tag = incoming.read(BACKUP_TAG_BYTES)
        ciphertext_end = incoming.tell() - BACKUP_TAG_BYTES
        incoming.seek(len(header))
        decryptor = Cipher(algorithms.AES(_backup_key(secret, salt)), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        with output.open("xb") as outgoing:
            remaining = ciphertext_end - len(header)
            while remaining:
                chunk = incoming.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("加密备份内容不完整")
                remaining -= len(chunk)
                outgoing.write(decryptor.update(chunk))
            outgoing.write(decryptor.finalize())
    output.chmod(0o600)


def create_backup(
    storage: Path, output: Path, database_path: Path | None = None, encryption_key: str | None = None,
) -> dict[str, object]:
    storage = storage.resolve()
    output = output.resolve()
    database = (database_path or storage / "meridian.sqlite3").resolve()
    if not database.is_file():
        raise FileNotFoundError(f"database does not exist: {database}")
    output.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0
    with tempfile.TemporaryDirectory(prefix="meridian-backup-") as temp_dir:
        snapshot = Path(temp_dir) / "meridian.sqlite3"
        plain_archive = Path(temp_dir) / "backup.tar.gz" if encryption_key else output
        with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        with tarfile.open(plain_archive, "w:gz") as archive:
            archive.add(snapshot, arcname="storage/meridian.sqlite3")
            file_count += 1
            for path in sorted(storage.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(storage)
                if relative.parts[0] == "backups" or path == output:
                    continue
                if path.name in {"meridian.sqlite3", "meridian.sqlite3-wal", "meridian.sqlite3-shm", ".instance.lock"}:
                    continue
                archive.add(path, arcname=Path("storage") / relative, recursive=False)
                file_count += 1
        if encryption_key:
            encrypt_backup(plain_archive, output, encryption_key)
        else:
            output.chmod(0o600)
    hasher = hashlib.sha256()
    with output.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    return {
        "path": str(output), "sha256": digest, "files": file_count,
        "bytes": output.stat().st_size, "encrypted": bool(encryption_key),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent Meridian storage backup")
    parser.add_argument("--storage", default=os.getenv("MERIDIAN_STORAGE_DIR", "storage"))
    parser.add_argument("--database")
    parser.add_argument("--output")
    args = parser.parse_args()
    storage = Path(args.storage)
    encryption_key = os.getenv("MERIDIAN_BACKUP_KEY", "").strip()
    if os.getenv("MERIDIAN_ENV", "development").lower() == "production" and not encryption_key:
        parser.error("生产环境必须配置 MERIDIAN_BACKUP_KEY")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = ".tar.gz.enc" if encryption_key else ".tar.gz"
    output = Path(args.output) if args.output else storage / "backups" / f"meridian-{timestamp}{suffix}"
    database = Path(args.database) if args.database else None
    print(json.dumps(create_backup(storage, output, database, encryption_key or None), ensure_ascii=False))


if __name__ == "__main__":
    main()
