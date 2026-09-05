from __future__ import annotations

import re
from pathlib import Path

import yaml
from flask import current_app

from ..core.database import Database


MAX_SKILL_BYTES = 100_000
MAX_PROMPT_CHARS = 50_000
MAX_DESCRIPTION_CHARS = 240
MAX_RESOURCE_FILES = 200
RESOURCE_DIRS = ("references", "scripts", "assets")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
FRONTMATTER_PATTERN = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)


class SkillError(ValueError):
    pass


def _db() -> Database:
    return current_app.extensions["meridian_db"]


def _candidate_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    return [
        *sorted(path for path in root.glob("*.md") if path.is_file() and not path.is_symlink()),
        *sorted(path for path in root.glob("*/SKILL.md") if path.is_file() and not path.is_symlink()),
    ]


def _resources(path: Path) -> list[dict]:
    if path.name.upper() != "SKILL.MD":
        return []
    resources = []
    for kind in RESOURCE_DIRS:
        root = path.parent / kind
        if not root.is_dir() or root.is_symlink():
            continue
        for item in sorted(root.rglob("*"), key=lambda value: str(value).lower()):
            if len(resources) >= MAX_RESOURCE_FILES:
                raise SkillError(f"Skill 资源文件超过 {MAX_RESOURCE_FILES} 个")
            if item.is_symlink() or not item.is_file():
                continue
            resources.append({
                "kind": kind, "path": item.relative_to(path.parent).as_posix(), "size": item.stat().st_size,
            })
    return resources


def parse_skill(path: Path, source: str) -> dict:
    try:
        if path.is_symlink():
            raise SkillError("不允许符号链接形式的 SKILL.md")
        if path.stat().st_size > MAX_SKILL_BYTES:
            raise SkillError(f"Skill 文件超过 {MAX_SKILL_BYTES} 字节")
        raw = path.read_text(encoding="utf-8").lstrip("\ufeff")
    except OSError as exc:
        raise SkillError(f"无法读取 Skill：{exc}") from exc
    match = FRONTMATTER_PATTERN.match(raw)
    if not match:
        raise SkillError("缺少 YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillError(f"YAML frontmatter 无效：{exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillError("frontmatter 必须是对象")
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    prompt = raw[match.end():].strip()
    allowed_tools = metadata.get("allowedTools", [])
    if not NAME_PATTERN.fullmatch(name):
        raise SkillError(f"Skill 名称无效：{name}")
    if not description or len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillError("Skill description 不能为空且不能超过 240 字符")
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise SkillError("Skill 指令不能为空且不能超过 50000 字符")
    if not isinstance(allowed_tools, list) or not all(isinstance(item, str) for item in allowed_tools):
        raise SkillError("allowedTools 必须是工具名数组")
    if len(set(allowed_tools)) != len(allowed_tools):
        raise SkillError("allowedTools 不能重复")
    icon = str(metadata.get("icon") or "🧩")
    if len(icon) > 8:
        raise SkillError("Skill 图标过长")
    return {
        "id": name, "name": name,
        "display_name": str(metadata.get("displayName") or metadata.get("title") or name),
        "description": description, "instruction": prompt, "prompt": prompt,
        "icon": icon, "allowed_tools": allowed_tools, "source": source,
        "path": str(path), "resources": _resources(path), "enabled": True,
    }


def _roots(workspace_id: str) -> list[tuple[Path, str]]:
    settings = current_app.config["SETTINGS"]
    return [
        (settings.root / "skills", "builtin"),
        (settings.storage_dir / "skills", "user"),
        (settings.workspace_dir / workspace_id / "skills", "workspace"),
    ]


def load_skills(workspace_id: str) -> tuple[list[dict], list[dict]]:
    merged: dict[str, dict] = {}
    diagnostics = []
    for root, source in _roots(workspace_id):
        seen = set()
        for path in _candidate_files(root):
            try:
                skill = parse_skill(path, source)
            except SkillError as exc:
                diagnostics.append({"path": str(path), "source": source, "error": str(exc)})
                continue
            if skill["name"] in seen:
                diagnostics.append({
                    "path": str(path), "source": source,
                    "error": f"同一来源存在重复 Skill：{skill['name']}",
                })
                continue
            seen.add(skill["name"])
            merged[skill["name"]] = skill
    for record in reversed(_db().list("skills", workspace_id=workspace_id, limit=5000)):
        if not record.get("enabled", True):
            continue
        name = str(record.get("slug") or record.get("name") or record["id"])
        merged[name] = {
            **record, "name": name, "display_name": record.get("display_name") or record.get("name") or name,
            "prompt": record.get("instruction", ""), "allowed_tools": record.get("allowed_tools", []),
            "source": "database", "resources": record.get("resources", []),
        }
    return list(merged.values()), diagnostics


def public_skill(skill: dict, *, include_prompt: bool = False) -> dict:
    hidden = {"path"}
    if not include_prompt:
        hidden.update({"instruction", "prompt"})
    return {key: value for key, value in skill.items() if key not in hidden}


def get_skill(name: str | None, workspace_id: str) -> dict | None:
    if not name:
        return None
    skills, _ = load_skills(workspace_id)
    return next((skill for skill in skills if name in {skill.get("id"), skill.get("name")}), None)


def read_skill_resource(skill_name: str, resource_path: str, workspace_id: str) -> tuple[Path, str]:
    skill = get_skill(skill_name, workspace_id)
    if not skill or not skill.get("path"):
        raise FileNotFoundError("Skill 不存在或没有文件资源")
    relative = Path(resource_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise PermissionError("Skill 资源路径越界")
    allowed = {item["path"] for item in skill.get("resources", [])}
    normalized = relative.as_posix()
    if normalized not in allowed:
        raise FileNotFoundError("Skill 资源不存在")
    root = Path(skill["path"]).parent.resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise PermissionError("Skill 资源路径越界")
    return target, target.suffix.lower()
