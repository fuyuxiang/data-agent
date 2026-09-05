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


def create_backup(
    storage: Path, output: Path, database_path: Path | None = None,
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
        with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        with tarfile.open(output, "w:gz") as archive:
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
    hasher = hashlib.sha256()
    with output.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    return {"path": str(output), "sha256": digest, "files": file_count, "bytes": output.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent Meridian storage backup")
    parser.add_argument("--storage", default=os.getenv("MERIDIAN_STORAGE_DIR", "storage"))
    parser.add_argument("--database")
    parser.add_argument("--output")
    args = parser.parse_args()
    storage = Path(args.storage)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(args.output) if args.output else storage / "backups" / f"meridian-{timestamp}.tar.gz"
    database = Path(args.database) if args.database else None
    print(json.dumps(create_backup(storage, output, database), ensure_ascii=False))


if __name__ == "__main__":
    main()
