from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class SandboxLimits:
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    timeout_seconds: int = 120
    output_bytes: int = 50 * 1024 * 1024


class SandboxUnavailable(RuntimeError):
    pass


class SandboxRunner:
    """Fixed rootless container runner.  It intentionally has no host-exec fallback."""

    def __init__(
        self,
        *,
        image: str,
        input_root: Path,
        output_root: Path,
        limits: SandboxLimits | None = None,
        docker_input_root: Path | None = None,
        docker_output_root: Path | None = None,
        docker_volume: str | None = None,
    ):
        if not image or image.endswith(":latest"):
            raise ValueError("sandbox 镜像必须使用固定版本，禁止 latest")
        self.image = image
        self.input_root = input_root.resolve()
        self.output_root = output_root.resolve()
        self.docker_input_root = (docker_input_root or self.input_root).resolve()
        self.docker_output_root = (docker_output_root or self.output_root).resolve()
        self.docker_volume = str(docker_volume or "")
        if self.docker_volume and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.docker_volume):
            raise ValueError("sandbox Docker volume 名称无效")
        if self.docker_volume and (docker_input_root or docker_output_root):
            raise ValueError("sandbox volume 和 host bind root 不得同时配置")
        self.limits = limits or SandboxLimits()

    def capability(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        server_version = ""
        error = "docker CLI is not installed"
        if docker:
            try:
                completed = subprocess.run(  # noqa: S603 -- fixed read-only Docker capability probe
                    [docker, "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True, text=True, timeout=3, check=False,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                )
                server_version = completed.stdout.strip() if completed.returncode == 0 else ""
                error = "" if server_version else (completed.stderr or "Docker daemon is unavailable")[-1000:].strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                error = str(exc)
        return {
            "available": bool(server_version),
            "backend": "rootless-container", "image": self.image,
            "network": "none", "host_fallback": False,
            "server_version": server_version or None, "error": error or None,
        }

    def execute(
        self,
        spec: dict[str, Any],
        *,
        input_dir: Path,
        run_id: str,
        should_cancel=None,
    ) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker or not self.capability()["available"]:
            raise SandboxUnavailable("隔离容器不可用；已拒绝在应用主进程执行生成代码")
        source = input_dir.resolve()
        if source != self.input_root and self.input_root not in source.parents:
            raise PermissionError("sandbox 输入路径越界")
        if not source.is_dir():
            raise FileNotFoundError("sandbox 输入目录不存在")
        input_relative = source.relative_to(self.input_root)
        source_for_docker = self.docker_input_root / input_relative
        output = (self.output_root / _safe(run_id)).resolve()
        if self.output_root not in output.parents:
            raise PermissionError("sandbox 输出路径越界")
        output.mkdir(parents=True, exist_ok=False)
        # The fixed non-root container identity needs write access only to this
        # one newly-created output directory.  No parent or host path is writable.
        output.chmod(0o733)
        output_relative = output.relative_to(self.output_root)
        output_for_docker = self.docker_output_root / output_relative
        if len(json.dumps(spec, ensure_ascii=False, default=str)) > 100_000:
            raise ValueError("sandbox JobSpec 超过大小限制")
        descriptor, raw_spec_file = tempfile.mkstemp(prefix="meridian-sandbox-", suffix=".json", dir=output)
        os.close(descriptor)
        spec_file = Path(raw_spec_file)
        container_name = f"meridian-sandbox-{_safe(run_id)}"
        try:
            spec_file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            spec_file.chmod(0o644)
            if self.docker_volume:
                input_mount = (
                    f"type=volume,src={self.docker_volume},dst=/input,"
                    f"volume-subpath=workspaces/sandbox-inputs/{input_relative.as_posix()},readonly"
                )
                output_mount = (
                    f"type=volume,src={self.docker_volume},dst=/output,"
                    f"volume-subpath=exports/sandbox/{output_relative.as_posix()}"
                )
            else:
                input_mount = f"type=bind,src={source_for_docker},dst=/input,readonly"
                output_mount = f"type=bind,src={output_for_docker},dst=/output"
            command = [
                docker, "run", "--rm", "--name", container_name,
                "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--memory", f"{self.limits.memory_mb}m", "--cpus", str(self.limits.cpus),
                "--pids-limit", str(self.limits.pids), "--user", "65534:65534",
                "--mount", input_mount,
                "--mount", output_mount,
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108 -- container-local tmpfs
                self.image, "python", "/opt/meridian/run_job.py", f"/output/{spec_file.name}",
            ]
            process = subprocess.Popen(  # noqa: S603 -- fixed executable/argv; model cannot set container flags
                command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
            deadline = time.monotonic() + self.limits.timeout_seconds
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    cancelled = bool(should_cancel and should_cancel())
                    timed_out = time.monotonic() >= deadline
                    output_exceeded = _directory_bytes(output) > self.limits.output_bytes
                    if not cancelled and not timed_out and not output_exceeded:
                        continue
                    subprocess.run(  # noqa: S603 -- exact container name generated by this runner
                        [docker, "stop", "--time", "1", container_name], capture_output=True,
                        text=True, timeout=5, check=False,
                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    )
                    process.kill()
                    process.communicate()
                    if cancelled:
                        raise InterruptedError("sandbox 作业已应用取消请求")
                    if output_exceeded:
                        raise ValueError("sandbox 输出超过大小限制并已终止")
                    raise TimeoutError("sandbox 作业超时并已终止")
            if process.returncode:
                raise RuntimeError((stderr or stdout or "sandbox failed")[-4000:])
            manifest_path = output / "manifest.json"
            if not manifest_path.is_file():
                raise RuntimeError("sandbox 未生成结果 manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = []
            total = 0
            declared: set[Path] = set()
            for raw in manifest.get("files") or []:
                relative = Path(str(raw.get("path") or ""))
                if relative.is_absolute() or ".." in relative.parts:
                    raise PermissionError("sandbox manifest 包含越界路径")
                target = (output / relative).resolve()
                if output not in target.parents or not target.is_file() or target.is_symlink():
                    raise PermissionError("sandbox manifest 引用了无效产物")
                size = target.stat().st_size
                total += size
                if total > self.limits.output_bytes:
                    raise ValueError("sandbox 输出超过大小限制")
                declared.add(target)
                files.append({
                    "path": relative.as_posix(), "bytes": size,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                })
            allowed = declared | {manifest_path, spec_file}
            emitted = {path.resolve() for path in output.rglob("*") if path.is_file() or path.is_symlink()}
            if any(path.is_symlink() for path in output.rglob("*")):
                raise PermissionError("sandbox 输出不得包含符号链接")
            undeclared = emitted - allowed
            if undeclared:
                raise PermissionError("sandbox 生成了未申报产物")
            return {
                "status": "SUCCEEDED", "output_dir": str(output), "files": files,
                "metrics": manifest.get("metrics") or {},
            }
        finally:
            spec_file.unlink(missing_ok=True)


def _safe(value: str) -> str:
    result = "".join(character for character in str(value) if character.isalnum() or character in "-_")
    if not result:
        raise ValueError("sandbox run id 无效")
    return result[:128]


def _directory_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total
