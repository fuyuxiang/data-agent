"""Restricted remote compute preflight used by trusted SSH connections.

This module deliberately exposes no command execution or training payload
surface. Deploy it on a GPU host and run ``python3 -m meridian_remote_runner
--preflight --json`` to verify that PyTorch can see CUDA before registering
that host in the workbench.
"""
from __future__ import annotations

import argparse
import json
import platform
from typing import Any


RUNNER_VERSION = "1.0"


def inspect_runtime() -> tuple[int, dict[str, Any]]:
    try:
        import torch
    except ImportError:
        return 2, {
            "status": "not_ready", "version": RUNNER_VERSION,
            "python": platform.python_version(), "message": "PyTorch 未安装",
        }
    if not torch.cuda.is_available():
        return 2, {
            "status": "not_ready", "version": RUNNER_VERSION,
            "python": platform.python_version(), "cuda": False,
            "message": "未检测到可用 CUDA",
        }
    count = int(torch.cuda.device_count())
    devices = []
    for index in range(count):
        properties = torch.cuda.get_device_properties(index)
        devices.append({
            "index": index, "name": str(properties.name),
            "memory_bytes": int(properties.total_memory),
            "capability": list(torch.cuda.get_device_capability(index)),
        })
    return 0, {
        "status": "ready", "version": RUNNER_VERSION,
        "python": platform.python_version(), "torch": str(torch.__version__),
        "cuda": True, "cuda_version": str(torch.version.cuda or ""),
        "device_count": count, "gpu_name": devices[0]["name"] if devices else "",
        "devices": devices,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Meridian restricted remote compute runner")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if not (args.preflight and args.as_json):
        parser.error("only --preflight --json is supported")
    status, payload = inspect_runtime()
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
