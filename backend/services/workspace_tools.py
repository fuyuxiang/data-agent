from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import pandas as pd
from docx import Document
from flask import current_app
from pypdf import PdfReader

from ..core.database import Database, utcnow


SKIPPED_PARTS = {".git", ".baa", "node_modules", "__pycache__", ".venv", "dist", "build"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html",
    ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".sql", ".toml", ".ini", ".cfg", ".log",
}
SHEET_SUFFIXES = {".xlsx", ".xls", ".ods"}


class WorkspaceFiles:
    def __init__(self, database: Database, workspace_id: str, read_paths: set[str], session_id: str = ""):
        self.database = database
        self.workspace_id = workspace_id
        self.read_paths = read_paths
        self.session_id = session_id
        self.settings = current_app.config["SETTINGS"]
        self.workspace = database.get("workspaces", workspace_id) or {}
        self.output_root = (self.settings.workspace_dir / workspace_id / "outputs").resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.mcp_root = (self.settings.workspace_dir / workspace_id / "mcp").resolve()
        self.mcp_root.mkdir(parents=True, exist_ok=True)
        mounted = self.workspace.get("mounted_path")
        self.user_root = Path(mounted).resolve() if mounted else None
        if session_id:
            self.read_paths.update(
                str(item.get("resolved_path"))
                for item in database.list("workspace_file_reads", workspace_id=workspace_id, limit=5000)
                if item.get("session_id") == session_id and item.get("resolved_path")
            )

    def _remember_read(self, path: Path) -> None:
        self.read_paths.add(str(path))
        if not self.session_id:
            return
        existing = next(
            (
                item for item in self.database.list("workspace_file_reads", workspace_id=self.workspace_id, limit=5000)
                if item.get("session_id") == self.session_id and item.get("resolved_path") == str(path)
            ),
            None,
        )
        record = {
            "id": existing["id"] if existing else self.database.new_id("read"),
            "workspace_id": self.workspace_id, "session_id": self.session_id,
            "resolved_path": str(path), "read_at": utcnow(),
        }
        self.database.put("workspace_file_reads", record, workspace_id=self.workspace_id)

    def _backup(self, path: Path, namespace: str, operation: str) -> dict | None:
        if not path.is_file() or path.is_symlink():
            return None
        history_id = self.database.new_id("filever")
        root = (self.settings.workspace_dir / self.workspace_id / "file_history").resolve()
        root.mkdir(parents=True, exist_ok=True)
        backup = root / history_id
        shutil.copy2(path, backup)
        return self.database.put(
            "file_history",
            {
                "id": history_id, "workspace_id": self.workspace_id,
                "session_id": self.session_id, "original_uri": self.uri(path, namespace),
                "backup_path": str(backup), "operation": operation, "size": backup.stat().st_size,
                "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(), "created_at": utcnow(),
            },
            workspace_id=self.workspace_id,
        )

    def _owned_system_file(self, path: Path) -> bool:
        for collection in ("sources", "artifacts", "knowledge_documents"):
            for item in self.database.list(collection, workspace_id=self.workspace_id, limit=5000):
                if item.get("path") and Path(item["path"]).resolve() == path:
                    return True
        return False

    def resolve(self, value: str, *, write: bool = False, must_exist: bool = True) -> tuple[Path, str]:
        raw = str(value or "").strip()
        roots: dict[str, Path | None] = {
            "user": self.user_root, "outputs": self.output_root,
            "uploads": self.settings.upload_dir.resolve(), "mcp": self.mcp_root,
        }
        namespace = "user"
        relative = raw
        if raw.startswith("workspace://"):
            suffix = raw[len("workspace://"):]
            namespace, _, relative = suffix.partition("/")
        if namespace not in roots:
            raise ValueError("未知工作区路径命名空间")
        root = roots[namespace]
        if root is None:
            raise ValueError("当前工作空间没有挂载用户目录")
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            raise ValueError("路径超出工作区允许范围")
        if write and namespace not in {"user", "outputs"}:
            raise PermissionError("该工作区路径只读")
        if namespace == "uploads" and path.is_file() and not self._owned_system_file(path):
            raise PermissionError("文件不属于当前工作空间")
        if must_exist and not path.exists():
            raise FileNotFoundError(f"文件不存在：{raw}")
        return path, namespace

    def uri(self, path: Path, namespace: str) -> str:
        root = {
            "user": self.user_root, "outputs": self.output_root,
            "uploads": self.settings.upload_dir.resolve(), "mcp": self.mcp_root,
        }[namespace]
        return f"workspace://{namespace}/{path.relative_to(root).as_posix()}"

    def status(self) -> dict:
        return {
            "workspace_id": self.workspace_id, "mounted": self.user_root is not None,
            "roots": {
                "user": "workspace://user/" if self.user_root else None,
                "outputs": "workspace://outputs/", "uploads": "workspace://uploads/",
                "mcp": "workspace://mcp/",
            },
            "read_before_edit": True, "max_text_bytes": 20 * 1024 * 1024,
        }

    def glob(self, pattern: str, base: str = "", limit: int = 100, cursor: int = 0) -> dict:
        if base:
            root, namespace = self.resolve(base, must_exist=True)
        elif self.user_root:
            root, namespace = self.user_root, "user"
        else:
            root, namespace = self.output_root, "outputs"
        if not root.is_dir():
            raise ValueError("搜索起点必须是目录")
        pattern = str(pattern or "*")[:300]
        values = []
        for path in root.glob(pattern):
            if any(part in SKIPPED_PARTS for part in path.parts) or path.is_symlink() or not path.is_file():
                continue
            values.append({
                "path": self.uri(path.resolve(), namespace), "name": path.name,
                "size": path.stat().st_size, "suffix": path.suffix.lower(),
            })
            if len(values) >= 5000:
                break
        values.sort(key=lambda item: item["path"])
        start, count = max(0, int(cursor)), max(1, min(int(limit), 100))
        return {"items": values[start:start + count], "next_cursor": start + count if start + count < len(values) else None}

    def read(self, file_path: str, *, offset: int = 0, limit: int = 400, sheet_name: str = "") -> dict:
        path, namespace = self.resolve(file_path)
        if not path.is_file() or path.is_symlink():
            raise ValueError("只能读取普通文件")
        size = path.stat().st_size
        if size > 256 * 1024 * 1024:
            raise ValueError("文件超过 256 MiB 读取上限")
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 400))
        suffix = path.suffix.lower()
        if suffix in SHEET_SUFFIXES:
            book = pd.ExcelFile(path)
            selected = sheet_name if sheet_name in book.sheet_names else book.sheet_names[0]
            frame = pd.read_excel(path, sheet_name=selected, skiprows=range(1, offset + 1), nrows=limit)
            content: Any = {
                "sheet": selected, "sheets": book.sheet_names, "columns": [str(value) for value in frame.columns],
                "rows": frame.where(pd.notna(frame), None).to_dict(orient="records"),
            }
        elif suffix == ".docx":
            content = "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
        elif suffix == ".pdf":
            content = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages[:100])
        elif suffix in TEXT_SUFFIXES or not suffix:
            raw = path.read_bytes()
            if len(raw) > 20 * 1024 * 1024:
                raise ValueError("文本文件超过 20 MiB 读取上限")
            text = None
            for encoding in ("utf-8", "utf-16", "gb18030"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                raise ValueError("文件不是可读文本")
            lines = text.splitlines()
            content = "\n".join(lines[offset:offset + limit])[:12000]
        else:
            raise ValueError(f"不支持读取该文件格式：{suffix}")
        self._remember_read(path)
        return {"path": self.uri(path, namespace), "size": size, "content": content, "offset": offset}

    def grep(self, pattern: str, base: str = "", include: str = "**/*", limit: int = 50) -> dict:
        if len(pattern) > 500:
            raise ValueError("正则表达式过长")
        expression = re.compile(pattern)
        candidates = self.glob(include, base, 100, 0)["items"]
        matches = []
        for item in candidates[:200]:
            if Path(item["name"]).suffix.lower() not in TEXT_SUFFIXES:
                continue
            path, _namespace = self.resolve(item["path"])
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if expression.search(line):
                    matches.append({"path": item["path"], "line": line_number, "text": line[:500]})
                    if len(matches) >= max(1, min(int(limit), 50)):
                        return {"items": matches, "truncated": True}
        return {"items": matches, "truncated": False}
