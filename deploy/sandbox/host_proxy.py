"""Socket-holding sandbox proxy. It accepts only authenticated bounded JobSpecs."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from flask import Flask, jsonify, request

from sandbox import SandboxLimits, SandboxRunner


TOKEN = os.environ.get("MERIDIAN_SANDBOX_PROXY_TOKEN", "")
LOCAL_STORAGE = Path(os.environ.get("MERIDIAN_SANDBOX_PROXY_STORAGE", "/data")).resolve()
STORAGE_VOLUME = os.environ.get("MERIDIAN_SANDBOX_STORAGE_VOLUME", "")
IMAGE = os.environ.get("MERIDIAN_SANDBOX_IMAGE", "meridian-sandbox:py311-20260906")
if len(TOKEN) < 32:
    raise RuntimeError("MERIDIAN_SANDBOX_PROXY_TOKEN must contain at least 32 characters")
if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", STORAGE_VOLUME):
    raise RuntimeError("MERIDIAN_SANDBOX_STORAGE_VOLUME must be an explicit Docker volume name")

INPUT_ROOT = LOCAL_STORAGE / "workspaces" / "sandbox-inputs"
OUTPUT_ROOT = LOCAL_STORAGE / "exports" / "sandbox"
INPUT_ROOT.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
RUNNER = SandboxRunner(
    image=IMAGE, input_root=INPUT_ROOT, output_root=OUTPUT_ROOT,
    docker_volume=STORAGE_VOLUME,
    limits=SandboxLimits(
        memory_mb=max(128, int(os.environ.get("MERIDIAN_SANDBOX_MEMORY_MB", "512"))),
        cpus=max(0.1, float(os.environ.get("MERIDIAN_SANDBOX_CPUS", "1"))),
        pids=max(16, int(os.environ.get("MERIDIAN_SANDBOX_PIDS", "128"))),
        timeout_seconds=max(5, int(os.environ.get("MERIDIAN_SANDBOX_TIMEOUT_SECONDS", "120"))),
    ),
)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
jobs: dict[str, threading.Event] = {}
lock = threading.Lock()


def authorized() -> bool:
    import hmac

    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return hmac.compare_digest(supplied.encode(), TOKEN.encode())


@app.get("/health")
def health():
    return jsonify(ok=True, service="meridian-sandbox-proxy")


@app.get("/v1/capability")
def capability():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    return jsonify(RUNNER.capability())


@app.post("/v1/jobs")
def execute():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get("run_id") or "")
    input_subdir = str(payload.get("input_subdir") or "")
    spec = payload.get("spec")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        return jsonify(error="invalid run_id"), 400
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", input_subdir) or not isinstance(spec, dict):
        return jsonify(error="invalid bounded JobSpec"), 400
    cancel = threading.Event()
    with lock:
        if run_id in jobs:
            return jsonify(error="run_id already active"), 409
        jobs[run_id] = cancel
    try:
        result = RUNNER.execute(
            spec, input_dir=INPUT_ROOT / input_subdir, run_id=run_id,
            should_cancel=cancel.is_set,
        )
        return jsonify({**result, "image": IMAGE})
    except InterruptedError as exc:
        return jsonify(error=str(exc), status="CANCELLED"), 409
    except Exception as exc:
        return jsonify(error=str(exc), status="FAILED"), 500
    finally:
        with lock:
            jobs.pop(run_id, None)


@app.delete("/v1/jobs/<run_id>")
def cancel(run_id: str):
    if not authorized():
        return jsonify(error="unauthorized"), 401
    with lock:
        event = jobs.get(run_id)
    if event:
        event.set()
    return jsonify(accepted=bool(event), run_id=run_id), 202 if event else 404


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8090, threads=8, channel_timeout=180)  # noqa: S104 -- internal Compose network
