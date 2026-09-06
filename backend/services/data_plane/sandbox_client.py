from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from .sandbox import SandboxUnavailable


class SandboxClient:
    """Authenticated client for the socket-holding, fixed-policy sandbox proxy."""

    def __init__(
        self, *, endpoint: str, token: str, input_root: Path, output_root: Path,
        timeout_seconds: int, expected_image: str,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.input_root = input_root.resolve()
        self.output_root = output_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.expected_image = expected_image
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise SandboxUnavailable("sandbox proxy URL 无效")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "sandbox-proxy"}:
            raise SandboxUnavailable("非本机 sandbox proxy 必须使用 HTTPS")
        if len(token) < 32:
            raise SandboxUnavailable("sandbox proxy 需要独立的至少 32 字符鉴权令牌")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def execute(self, spec: dict[str, Any], *, input_dir: Path, run_id: str, should_cancel=None) -> dict[str, Any]:
        source = input_dir.resolve()
        if source.parent != self.input_root or not source.is_dir():
            raise PermissionError("sandbox client 输入必须是授权根下的单个任务目录")
        payload = {"run_id": run_id, "input_subdir": source.name, "spec": spec}
        if len(json.dumps(payload, ensure_ascii=False)) > 100_000:
            raise ValueError("sandbox JobSpec 超过大小限制")
        responses: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            session = requests.Session()
            session.trust_env = False
            try:
                response = session.post(
                    f"{self.endpoint}/v1/jobs", headers=self.headers, json=payload,
                    timeout=(5, self.timeout_seconds + 15),
                )
                if len(response.content) > 2 * 1024 * 1024:
                    responses.put(("error", RuntimeError("sandbox proxy 响应超过 2 MiB")))
                elif response.status_code >= 400:
                    detail = (response.json().get("error") if "json" in response.headers.get("Content-Type", "") else response.text)
                    responses.put(("error", RuntimeError(str(detail or "sandbox proxy 失败"))))
                else:
                    responses.put(("ok", response.json()))
            except Exception as exc:  # network errors are surfaced as a failed Action
                responses.put(("error", exc))
            finally:
                session.close()

        worker = threading.Thread(target=invoke, name=f"sandbox-client-{run_id[:32]}", daemon=True)
        worker.start()
        deadline = time.monotonic() + self.timeout_seconds + 20
        while worker.is_alive():
            if should_cancel and should_cancel():
                self.cancel(run_id)
                worker.join(timeout=10)
                raise InterruptedError("sandbox 作业已请求取消")
            if time.monotonic() >= deadline:
                self.cancel(run_id)
                raise TimeoutError("sandbox proxy 响应超时")
            worker.join(timeout=0.25)
        kind, value = responses.get_nowait()
        if kind == "error":
            if isinstance(value, requests.RequestException):
                raise SandboxUnavailable(f"sandbox proxy 不可用：{value}") from value
            raise value
        if value.get("image") != self.expected_image:
            raise RuntimeError("sandbox proxy 返回的固定镜像版本与应用配置不一致")
        output = (self.output_root / run_id).resolve()
        if output.parent != self.output_root or not output.is_dir():
            raise RuntimeError("sandbox proxy 未在共享受管目录生成结果")
        return {**value, "output_dir": str(output)}

    def cancel(self, run_id: str) -> None:
        session = requests.Session()
        session.trust_env = False
        try:
            session.delete(
                f"{self.endpoint}/v1/jobs/{run_id}", headers=self.headers, timeout=(3, 5),
            )
        except requests.RequestException:
            pass
        finally:
            session.close()

    def capability(self) -> dict[str, Any]:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(f"{self.endpoint}/v1/capability", headers=self.headers, timeout=(3, 5))
            return response.json() if response.ok else {"available": False, "error": response.text[:1000]}
        except (requests.RequestException, ValueError) as exc:
            return {"available": False, "error": str(exc), "host_fallback": False}
        finally:
            session.close()
