"""Load builtin Skill packages from disk.

A builtin package is a directory under ``settings.skill_dir``::

    skills/data-analysis/
        SKILL.md          # human/模型可读的使用说明
        manifest.yaml     # machine-readable definition
        references/       # optional supporting material
        templates/        # optional output templates
        evals/            # optional example questions

Loading is deliberately forgiving: a malformed package is skipped and reported
rather than crashing the whole workspace, because a single bad file must not
take the Agent runtime down.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

from .models import SkillDefinition, SkillError, validate_id, validate_references

_CACHE: dict[str, tuple[float, dict[str, SkillDefinition]]] = {}
_LOCK = threading.RLock()

# Directories that may sit inside a package without being a package themselves.
_IGNORED = {"__pycache__", "node_modules", ".git", "evals", "references", "templates", "assets"}


def skill_dir() -> Path:
    """Resolve the package root, preferring the running app's configuration."""
    try:
        from flask import current_app

        configured = current_app.config.get("SETTINGS")
        if configured is not None:
            return Path(configured.skill_dir)
    except (ImportError, RuntimeError):
        pass
    return Path(__file__).resolve().parents[2] / "skills"


def _mtime(directory: Path) -> float:
    newest = 0.0
    try:
        for path in directory.rglob("*"):
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
    except OSError:
        return newest
    return newest


def read_package(directory: Path) -> SkillDefinition:
    """Parse one package directory into a :class:`SkillDefinition`."""
    manifest_path = directory / "manifest.yaml"
    if not manifest_path.is_file():
        raise SkillError(f"技能包缺少 manifest.yaml：{directory.name}")
    payload: Any = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SkillError(f"技能包 manifest.yaml 必须是对象：{directory.name}")
    if not str(payload.get("instruction") or "").strip():
        skill_md = directory / "SKILL.md"
        if skill_md.is_file():
            payload["instruction"] = skill_md.read_text(encoding="utf-8").strip()
    definition = SkillDefinition(
        id=validate_id(payload.get("id") or directory.name),
        name=str(payload.get("name") or directory.name),
        description=str(payload.get("description") or ""),
        category=str(payload.get("category") or "自定义"),
        version=str(payload.get("version") or "1.0.0"),
        status="published",
        source=str(payload.get("source") or "builtin"),
        instruction=str(payload.get("instruction") or ""),
        usage=str(payload.get("usage") or payload.get("description") or ""),
        triggers=tuple(str(item) for item in payload.get("triggers") or ()),
        example_questions=tuple(str(item) for item in payload.get("example_questions") or ()),
        allowed_tools=tuple(str(item) for item in payload.get("allowed_tools") or ()),
        source_ids=(),
        knowledge_document_ids=(),
        mcp_server_ids=tuple(str(item) for item in payload.get("mcp_server_ids") or ()),
        metric_names=tuple(str(item) for item in payload.get("metric_names") or ()),
        inputs=tuple(str(item) for item in payload.get("inputs") or ()),
        outputs=tuple(str(item) for item in payload.get("outputs") or ()),
        notes=str(payload.get("notes") or ""),
        agent_ids=(),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        published_at=str(payload.get("published_at") or ""),
        package_path=str(directory),
        reference_documents=validate_references({
            path.name: path.read_text(encoding="utf-8")
            for path in (directory / "references").glob("*.md")
            if not path.is_symlink() and path.stat().st_size <= 100_000
        } if not (directory / "references").is_symlink() else {}),
    )
    if not definition.instruction:
        raise SkillError(f"技能包缺少使用说明（SKILL.md 或 manifest.instruction）：{directory.name}")
    return definition


def load_builtin_skills(directory: Path | None = None, *, refresh: bool = False) -> dict[str, SkillDefinition]:
    """Return every readable builtin package keyed by skill id."""
    root = Path(directory) if directory is not None else skill_dir()
    if not root.is_dir():
        return {}
    stamp = _mtime(root)
    with _LOCK:
        cached = _CACHE.get(str(root))
        if not refresh and cached is not None and cached[0] == stamp:
            return cached[1]

    loaded: dict[str, SkillDefinition] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name in _IGNORED or entry.name.startswith((".", "_")):
            continue
        if not (entry / "manifest.yaml").is_file():
            # Not a skill package; the directory is simply not a skill.
            continue
        try:
            definition = read_package(entry)
        except (SkillError, OSError, yaml.YAMLError) as exc:
            logging.getLogger(__name__).warning("skip_broken_skill_package: %s", exc)
            continue
        loaded[definition.id] = definition
    with _LOCK:
        _CACHE[str(root)] = (stamp, loaded)
    return loaded


def reference_index(definition: SkillDefinition) -> list[dict[str, Any]]:
    """Expose a small index, never inject whole reference books into context."""
    entries = []
    for name, content in sorted(definition.reference_documents.items()):
        headings = [line.lstrip("# ").strip() for line in content.splitlines()
                    if line.startswith("## ")][:30]
        entries.append({"name": name, "sections": headings})
    return entries


def read_reference(definition: SkillDefinition, name: str, *, section: str = "",
                   offset: int = 0, limit: int = 100) -> dict[str, Any]:
    available = {item["name"] for item in reference_index(definition)}
    if name not in available:
        raise ValueError("技能参考文档不存在；请使用参考目录中的文件名")
    # Content is captured with the published definition, including imported
    # workspace skills. No model-controlled filesystem path is ever opened.
    lines = definition.reference_documents[name].splitlines()
    if section:
        starts = [index for index, line in enumerate(lines)
                  if line.startswith("## ") and line.lstrip("# ").strip() == section]
        if not starts:
            raise ValueError("参考文档章节不存在，请使用目录中的章节名")
        start = starts[0]
        end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
        lines = lines[start:end]
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
    selected, size = [], 0
    for line in lines[offset:offset + limit]:
        if size + len(line) > 12_000:
            if not selected:
                raise ValueError("参考文档单行过长")
            break
        selected.append(line)
        size += len(line) + 1
    end = offset + len(selected)
    return {"skill_id": definition.id, "reference": name, "section": section,
            "content": "\n".join(selected), "next_offset": end if end < len(lines) else None}
