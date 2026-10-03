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

from .models import SkillDefinition, SkillError, validate_id

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
