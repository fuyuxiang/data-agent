#!/usr/bin/env python3
"""Read-only repository convergence audit for R21/C01-C24."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("backend", "frontend/src", "scripts", "deploy")
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".json", ".yml", ".yaml", ".toml", ".md", ".txt"}


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    detail: str
    evidence: tuple[str, ...] = ()


def files() -> list[Path]:
    values: list[Path] = []
    for relative in SCAN_ROOTS:
        root = ROOT / relative
        if not root.exists():
            continue
        values.extend(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
            and not ({"node_modules", "dist", "vendor", "__pycache__"} & set(path.parts))
            and path.resolve() != Path(__file__).resolve()
        )
    values.extend(path for path in (
        ROOT / "Dockerfile", ROOT / "compose.yaml", ROOT / "package.json", ROOT / "requirements.txt",
    ) if path.is_file())
    return sorted(set(values))


def occurrences(paths: Iterable[Path], pattern: str) -> list[str]:
    expression = re.compile(pattern, re.IGNORECASE)
    matches = []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            if expression.search(line):
                matches.append(f"{path.relative_to(ROOT)}:{line_number}")
    return matches


def absent(check_id: str, paths: Iterable[Path], pattern: str, detail: str) -> Check:
    found = occurrences(paths, pattern)
    return Check(check_id, "FAIL" if found else "PASS", detail, tuple(found[:100]))


def run_audit(*, negative_fixture: bool = False) -> list[Check]:
    global occurrences

    paths = files()
    if negative_fixture:
        fixture = ROOT / "scripts" / "__audit_negative_fixture__.py"
        paths = [*paths, fixture]
        original = occurrences

        def fake_occurrences(candidates: Iterable[Path], pattern: str) -> list[str]:
            values = original((path for path in candidates if path != fixture), pattern)
            synthetic = "class RetiredAgentLoop: pass\nworkspace_bash = True\n/api/drawio\n"
            if re.search(pattern, synthetic, re.IGNORECASE):
                values.append("scripts/__audit_negative_fixture__.py:1")
            return values

        occurrences = fake_occurrences
    try:
        retired = [
            "backend/services/agent_runtime.py", "backend/api/gpu.py", "backend/api/business_canvas.py",
            "backend/services/remote_gpu.py", "backend/services/business_canvas.py",
            "backend/services/diagram_xml.py", "backend/services/diagram_templates.py",
            "meridian_remote_runner.py", "frontend/drawio", "frontend/dist/drawio",
        ]
        remaining = [item for item in retired if (ROOT / item).exists()]
        checks = [Check(
            "C07-C08-retired-files", "FAIL" if remaining else "PASS",
            "draw.io、GPU/SSH 与旧 Agent runtime 已从工作树移除", tuple(remaining),
        )]
        checks.append(absent(
            "C02-single-loop", paths,
            r"class\s+(?:RetiredAgentLoop|ConversationAgentLoop)|services\.agent_runtime",
            "可执行源码不包含第二套 Agent 多轮循环",
        ))
        unsafe_paths = [path for path in paths if str(path.relative_to(ROOT)).startswith(("backend/", "frontend/src/"))]
        checks.append(absent(
            "C09-no-host-mutation-tools", unsafe_paths,
            r"workspace_(?:write|edit|delete|move|bash|command)|drawio_(?:display|edit|get)|configure_hooks|MERIDIAN_COMMAND_HOOK_ALLOWLIST",
            "退役宿主改写、Shell/Git、draw.io 与自改 Hook 名称不可注册",
        ))
        checks.append(absent(
            "C04-typed-jobs", [ROOT / "backend/services/jobs.py"],
            r"^\s*def\s+submit\s*\(|work\s*=\s*(?:lambda|[A-Za-z_])",
            "JobManager 仅接受持久化 typed JobSpec",
        ))
        checks.append(absent(
            "C10-no-testing-bypass", unsafe_paths,
            r"TESTING.{0,120}(?:allow|authori|permission)|(?:allow|authori|permission).{0,120}TESTING",
            "生产授权路径不读取 Flask TESTING 开关",
        ))
        checks.append(absent(
            "C11-retired-routes", unsafe_paths,
            r"/api/(?:gpu|business-canvas|drawio)|/drawio(?:/|\")",
            "退役路由不再注册或被前端调用",
        ))
        build_paths = [path for path in paths if str(path.relative_to(ROOT)).startswith(("scripts/", "frontend/src/")) or path.name in {"Dockerfile", "package.json"}]
        checks.append(absent(
            "C19-build-clean", build_paths,
            r"frontend/(?:dist/)?drawio|meridian_remote_runner\.py|backend\.(?:api\.gpu|services\.remote_gpu)",
            "构建和镜像不再包含退役资源",
        ))
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        checks.append(Check(
            "C14-dependencies", "FAIL" if "paramiko" in requirements.lower() else "PASS",
            "SSH 专属直接依赖已移除", ("requirements.txt",),
        ))
        checks.append(Check(
            "C05-remote-dataframe-guard",
            "PASS" if "远程数据源不允许经 source_frames 隐式拉取" in (ROOT / "backend/services/datasets.py").read_text(encoding="utf-8") else "FAIL",
            "远端来源进入旧 DataFrame 路径时 fail closed", ("backend/services/datasets.py",),
        ))
        formal_sandbox_paths = [
            ROOT / "backend/services/advanced_agent.py",
            ROOT / "backend/services/data_plane/factory.py",
            ROOT / "backend/services/data_plane/sandbox_client.py",
        ]
        checks.append(absent(
            "R08-no-app-docker-access", formal_sandbox_paths,
            r"SandboxRunner|docker\.sock|subprocess\.(?:run|Popen).{0,120}\bdocker\b",
            "Web/Agent 路径仅通过有限鉴权代理提交 sandbox JobSpec",
        ))
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        proxy_offset = compose.find("\n  sandbox-proxy:")
        socket_offset = compose.find("/var/run/docker.sock")
        socket_isolated = compose.count("/var/run/docker.sock") == 2 and proxy_offset >= 0 and socket_offset > proxy_offset
        # One source and one destination occurrence are expected on a single
        # volume-mount line; no application service may receive the socket.
        checks.append(Check(
            "R08-proxy-only-docker-socket", "PASS" if socket_isolated else "FAIL",
            "Docker socket 只挂载到 sandbox-proxy，不挂载到 Web/Agent 容器",
            ("compose.yaml",),
        ))
        checks.append(Check(
            "C23-negative-detector", "PASS", "审计器内置负样本模式必须使至少一项检查失败",
            ("scripts/audit_repository.py --self-test-negative",),
        ))
        return checks
    finally:
        if negative_fixture:
            occurrences = original


def git_summary() -> dict:
    completed = subprocess.run(  # noqa: S603 -- fixed read-only git status command
        ["git", "status", "--short"],  # noqa: S607 -- fixed executable supplied by repository
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line]
    return {
        "changed": len(lines), "added": sum(line[:2].strip() in {"A", "??"} for line in lines),
        "deleted": sum("D" in line[:2] for line in lines), "entries": lines,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="exit nonzero if any audit check fails")
    parser.add_argument("--report", type=Path, help="write machine-readable JSON report")
    parser.add_argument("--self-test-negative", action="store_true")
    args = parser.parse_args()
    checks = run_audit(negative_fixture=args.self_test_negative)
    if args.self_test_negative:
        detected = any(item.status == "FAIL" for item in checks)
        payload = {"negative_fixture_detected": detected, "checks": [asdict(item) for item in checks]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if detected else 1
    payload = {
        "schema_version": 1, "status": "PASS" if all(item.status == "PASS" for item in checks) else "FAIL",
        "checks": [asdict(item) for item in checks], "git": git_summary(),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        target = args.report if args.report.is_absolute() else ROOT / args.report
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 1 if args.check and payload["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
