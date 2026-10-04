"""Run reviewed bounded analysis methods outside the web process."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .reviewed_analysis import METHODS


class LocalAnalysisRunner:
    def __init__(
        self, *, input_root: Path, output_root: Path,
        timeout_seconds: int = 120, output_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.input_root = input_root.resolve()
        self.output_root = output_root.resolve()
        self.timeout_seconds = max(5, timeout_seconds)
        self.output_bytes = output_bytes

    def capability(self) -> dict[str, Any]:
        return {"available": True, "backend": "reviewed-local-worker", "generated_code": False}

    def execute(
        self, spec: dict[str, Any], *, input_dir: Path, run_id: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        source_dir = input_dir.resolve()
        if source_dir.parent != self.input_root or not source_dir.is_dir():
            raise PermissionError("分析输入必须位于受管任务目录")
        filename = str(spec.get("input") or "")
        if not filename or Path(filename).name != filename:
            raise ValueError("分析输入文件名无效")
        source = source_dir / filename
        if not source.is_file() or source.is_symlink() or source.suffix not in {".csv", ".parquet"}:
            raise ValueError("分析输入必须是受管的 CSV 或 Parquet 文件")
        method = str(spec.get("method") or "")
        if method not in METHODS or spec.get("code"):
            raise ValueError("仅允许固定的审核分析方法，不执行生成的 Python 代码")
        params = spec.get("parameters") or {}
        if not isinstance(params, dict):
            raise ValueError("分析参数必须是对象")
        encoded_params = json.dumps(params, ensure_ascii=False)
        if len(encoded_params.encode("utf-8")) > 100_000:
            raise ValueError("分析参数超过大小限制")
        safe_id = "".join(char for char in run_id if char.isalnum() or char in "-_")[:128]
        if not safe_id:
            raise ValueError("分析任务标识无效")
        self.output_root.mkdir(parents=True, exist_ok=True)
        output = self.output_root / safe_id
        output.mkdir(exist_ok=False)
        target = output / "result.parquet"
        worker = Path(__file__).with_name("reviewed_analysis.py")
        process = subprocess.Popen(  # noqa: S603 -- fixed trusted worker and method allowlist
            [sys.executable, str(worker), str(source), str(target), method, encoded_params],
            shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONNOUSERSITE": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
            },
        )
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                cancelled = bool(should_cancel and should_cancel())
                timed_out = time.monotonic() >= deadline
                oversized = _directory_bytes(output) > self.output_bytes
                if not (cancelled or timed_out or oversized):
                    continue
                process.kill()
                process.communicate()
                if cancelled:
                    raise InterruptedError("分析任务已取消")
                if oversized:
                    raise ValueError("分析产物超过大小限制")
                raise TimeoutError("分析任务超时")
        if process.returncode:
            raise RuntimeError((stderr or stdout or "分析执行失败")[-4000:])
        manifest_path = output / "manifest.json"
        if not manifest_path.is_file() or not target.is_file():
            raise RuntimeError("分析未生成完整结果")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("files") != [{"path": "result.parquet"}] or not isinstance(manifest.get("metrics"), dict):
            raise RuntimeError("分析结果清单无效")
        emitted = list(output.iterdir())
        if any(path.is_symlink() for path in emitted) or set(emitted) != {manifest_path, target}:
            raise RuntimeError("分析产物包含未声明文件")
        size = target.stat().st_size
        if size > self.output_bytes:
            raise ValueError("分析产物超过大小限制")
        return {
            "status": "SUCCEEDED", "output_dir": str(output),
            "files": [{
                "path": target.name, "bytes": size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }],
            "metrics": manifest["metrics"], "backend": "reviewed-local-worker",
        }


def _directory_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total
