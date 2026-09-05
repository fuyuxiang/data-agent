#!/usr/bin/env python3
"""Build a secret-free, allowlisted source tree for desktop packaging."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


ALLOWED_FILES = ("app.py", "baa_remote_runner.py", "README.md", "THIRD_PARTY_NOTICES.md")
ALLOWED_TREES = ("backend", "frontend", "skills", "deploy", "packaging")
IGNORED_PARTS = {
    "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "dist", "build",
}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".log", ".sqlite", ".sqlite3", ".db"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(rb"\bgh[oprsu]_[A-Za-z0-9]{30,}\b"),
)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_files(root: Path):
    for tree_name in ALLOWED_TREES:
        tree = root / tree_name
        if not tree.is_dir() or tree.is_symlink():
            raise ValueError(f"打包必需目录不存在或不安全：{tree_name}")
        for item in sorted(tree.rglob("*")):
            relative = item.relative_to(root)
            if item.is_symlink():
                raise ValueError(f"打包源中不允许符号链接：{relative}")
            if not item.is_file():
                continue
            if any(part in IGNORED_PARTS for part in relative.parts) or item.suffix.lower() in IGNORED_SUFFIXES:
                continue
            if item.name == ".env" or item.name.startswith(".env."):
                raise ValueError(f"打包源包含环境密钥文件：{relative}")
            data = item.read_bytes() if item.stat().st_size <= 8 * 1024 * 1024 else b""
            if data and any(pattern.search(data) for pattern in SECRET_PATTERNS):
                raise ValueError(f"打包源疑似包含密钥：{relative}")
            yield item, relative
    for name in ALLOWED_FILES:
        item = root / name
        if not item.is_file() or item.is_symlink():
            raise ValueError(f"打包必需文件不存在或不安全：{name}")
        yield item, Path(name)


def build_staging(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if destination.exists():
        raise ValueError(f"打包目录已存在：{destination}")
    if destination == source:
        raise ValueError("打包目录不能覆盖项目源码")
    if source in destination.parents and destination.relative_to(source).parts[0] != "build":
        raise ValueError("项目内的打包目录必须位于 build/ 下")
    entries = list(_safe_files(source))
    destination.mkdir(parents=True)
    manifest = []
    for item, relative in entries:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        manifest.append({
            "path": relative.as_posix(), "size": target.stat().st_size, "sha256": _digest(target),
        })
    result = {
        "schema_version": 1, "file_count": len(manifest),
        "total_bytes": sum(item["size"] for item in manifest), "files": manifest,
    }
    (destination.parent / f"{destination.name}-manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build_staging(args.source, args.destination)
    except (OSError, ValueError) as exc:
        print(f"[packaging] blocked: {exc}")
        return 2
    print(f"[packaging] staged {result['file_count']} files ({result['total_bytes']} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
