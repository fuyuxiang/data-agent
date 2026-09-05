from __future__ import annotations

import os
from pathlib import Path
from typing import IO


def acquire_instance_lock(path: Path) -> IO[str]:
    """Hold an exclusive process lock for the single-writer SQLite deployment."""
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        raise RuntimeError(
            "当前存储目录已有 Meridian 实例运行；SQLite 部署仅支持单实例",
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle
