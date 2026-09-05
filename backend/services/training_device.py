from __future__ import annotations

from typing import Any, Iterable


def select_training_target(
    remote_runners: Iterable[dict[str, Any]] = (), *, min_free_memory_mb: int = 2048,
) -> dict[str, Any]:
    """Choose CUDA only when PyTorch confirms that it is usable; otherwise use CPU."""
    if min_free_memory_mb < 0:
        raise ValueError("min_free_memory_mb 必须非负")
    try:
        import torch
    except ImportError:
        return {"kind": "cpu", "device": "cpu", "reason": "PyTorch 未安装"}
    if torch.cuda.is_available():
        best = None
        for index in range(torch.cuda.device_count()):
            try:
                free_bytes, _total_bytes = torch.cuda.mem_get_info(index)
                free_mb = int(free_bytes / 1024 / 1024)
            except (RuntimeError, TypeError):
                free_mb = min_free_memory_mb
            if best is None or free_mb > best[1]:
                best = (index, free_mb)
        if best and best[1] >= min_free_memory_mb:
            return {
                "kind": "local", "device": f"cuda:{best[0]}",
                "reason": f"CUDA 可用，剩余显存 {best[1]} MB",
            }
    for runner in remote_runners:
        if runner.get("runner_ready") and runner.get("id"):
            return {
                "kind": "remote", "runner_id": runner["id"],
                "reason": f"使用已就绪远程训练器 {runner.get('name') or runner['id']}",
            }
    return {"kind": "cpu", "device": "cpu", "reason": "CUDA 不可用，使用 CPU"}
