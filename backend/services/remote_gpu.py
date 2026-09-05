from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import platform
import re
import select
import shutil
import socketserver
import subprocess
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from flask import current_app

from ..core.database import Database
from .security import SecretVault, safe_http_request, validate_outbound_url


log = logging.getLogger(__name__)
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_PREFLIGHT_COMMAND = "python3 -m baa_remote_runner --preflight --json"


class TunnelError(RuntimeError):
    pass


class UnknownHostKeyError(TunnelError):
    pass


def _paramiko():
    try:
        return importlib.import_module("paramiko")
    except ImportError as exc:  # pragma: no cover - declared runtime dependency
        raise TunnelError("未安装远程连接依赖 paramiko") from exc


def _port(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是 1 到 65535 的整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 1 到 65535 的整数") from exc
    if not 1 <= result <= 65535:
        raise ValueError(f"{field} 必须是 1 到 65535 的整数")
    return result


def _host(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result or not _HOST_RE.fullmatch(result):
        raise ValueError(f"{field} 格式无效")
    return result


def _base_url(value: object) -> str:
    result = str(value or "").strip().rstrip("/")
    parsed = urlsplit(result)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url 必须是不含凭据的有效 HTTP(S) 地址")
    is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not is_loopback:
        raise ValueError("公网直连必须使用 HTTPS；仅 localhost 可使用 HTTP")
    if not is_loopback:
        validate_outbound_url(result)
    return result


def public_connection(item: dict) -> dict:
    value = dict(item)
    value.pop("credential", None)
    value["has_password"] = bool(item.get("credential"))
    value["connected"] = connection_manager.status(item["id"]).get("connected", False)
    if value.get("connection_type") == "direct":
        value["connected"] = False  # Direct endpoints are probed on demand, not assumed alive.
    return value


def list_connections(database: Database, workspace_id: str) -> list[dict]:
    return [public_connection(item) for item in database.list("gpu_connections", workspace_id=workspace_id, limit=5000)]


def create_connection(database: Database, workspace_id: str, payload: dict) -> dict:
    connection_type = str(payload.get("connection_type") or "ssh").strip()
    if connection_type not in {"ssh", "direct"}:
        raise ValueError("connection_type 必须是 ssh 或 direct")
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("name 不能为空且最长 80 个字符")
    record: dict[str, Any] = {
        "id": database.new_id("gpu"), "workspace_id": workspace_id,
        "name": name, "connection_type": connection_type, "status": "configured",
    }
    if connection_type == "direct":
        record["base_url"] = _base_url(payload.get("base_url"))
    else:
        auth_method = str(payload.get("auth_method") or "agent").strip()
        if auth_method not in {"agent", "password", "key_file"}:
            raise ValueError("auth_method 必须是 agent、password 或 key_file")
        username = str(payload.get("username") or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        record.update({
            "host": _host(payload.get("host"), "host"),
            "port": _port(payload.get("port", 22), "port"), "username": username[:128],
            "target_host": _host(payload.get("target_host", "127.0.0.1"), "target_host"),
            "target_port": _port(payload.get("target_port"), "target_port"),
            "auth_method": auth_method,
        })
        if auth_method == "password":
            password = str(payload.get("password") or "")
            if not password:
                raise ValueError("密码认证必须提供 password")
            record["credential"] = SecretVault(current_app.config["SECRET_KEY"]).seal({"password": password})
        elif auth_method == "key_file":
            key_file = str(payload.get("key_file") or "").strip()
            if not key_file:
                raise ValueError("key_file 不能为空")
            record["key_file"] = key_file[:1000]
    return public_connection(database.put("gpu_connections", record, workspace_id=workspace_id))


def fingerprint(key: Any) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def inspect_host_key(connection: dict, timeout: float = 8.0) -> dict[str, str]:
    if connection.get("connection_type") != "ssh":
        raise ValueError("公网直连不使用 SSH 主机指纹")
    paramiko = _paramiko()
    transport = paramiko.Transport((connection["host"], connection["port"]))
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        return {"type": key.get_name(), "fingerprint": fingerprint(key), "base64": key.get_base64()}
    finally:
        transport.close()


def _decode_key(key_type: str, key_base64: str):
    paramiko = _paramiko()
    try:
        return paramiko.PKey.from_type_string(key_type, base64.b64decode(key_base64, validate=True))
    except Exception as exc:
        raise TunnelError("无效的 SSH 主机公钥") from exc


def trust_host_key(database: Database, connection: dict, key_type: str, key_base64: str) -> dict:
    if connection.get("connection_type") != "ssh":
        raise ValueError("公网直连不使用 SSH 主机指纹")
    key = _decode_key(str(key_type or ""), str(key_base64 or ""))
    return database.put(
        "ssh_host_keys",
        {
            "id": f"hostkey_{connection['id']}", "workspace_id": connection["workspace_id"],
            "connection_id": connection["id"], "host": connection["host"], "port": connection["port"],
            "key_type": key.get_name(), "key_base64": key.get_base64(), "fingerprint": fingerprint(key),
        },
        workspace_id=connection["workspace_id"],
    )


class _ForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        tunnel = self.server.tunnel  # type: ignore[attr-defined]
        transport = tunnel.client.get_transport()
        if not transport or not transport.is_active():
            return
        channel = transport.open_channel(
            "direct-tcpip", (tunnel.target_host, tunnel.target_port), self.request.getpeername(),
        )
        if channel is None:
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, channel], [], [], 1.0)
                if self.request in readable:
                    data = self.request.recv(32768)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(32768)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()


@dataclass
class SshTunnel:
    connection_id: str
    client: Any
    server: _ForwardServer
    target_host: str
    target_port: int

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{int(self.server.server_address[1])}"

    def healthy(self) -> bool:
        transport = self.client.get_transport()
        return bool(transport and transport.is_active())

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.client.close()


class ConnectionManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tunnels: dict[str, SshTunnel] = {}

    def connect(self, connection: dict, trusted_key: dict, password: str | None = None) -> SshTunnel:
        if not trusted_key:
            raise UnknownHostKeyError("SSH 主机尚未确认；请先核对并信任主机指纹")
        paramiko = _paramiko()
        with self._lock:
            self.close(connection["id"])
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            host_name = connection["host"] if connection["port"] == 22 else f"[{connection['host']}]:{connection['port']}"
            key = _decode_key(trusted_key["key_type"], trusted_key["key_base64"])
            client.get_host_keys().add(host_name, trusted_key["key_type"], key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            try:
                client.connect(
                    hostname=connection["host"], port=connection["port"], username=connection["username"],
                    password=password, key_filename=connection.get("key_file"), timeout=10,
                    banner_timeout=10, auth_timeout=10, look_for_keys=password is None,
                    allow_agent=password is None,
                )
            except paramiko.SSHException as exc:
                client.close()
                if "known_hosts" in str(exc).lower() or "host key" in str(exc).lower():
                    raise UnknownHostKeyError("SSH 主机指纹未信任或已变更") from exc
                raise TunnelError(f"SSH 连接失败: {exc}") from exc
            except OSError as exc:
                client.close()
                raise TunnelError(f"SSH 网络连接失败: {exc}") from exc
            server = _ForwardServer(("127.0.0.1", 0), _ForwardHandler)
            tunnel = SshTunnel(connection["id"], client, server, connection["target_host"], connection["target_port"])
            server.tunnel = tunnel  # type: ignore[attr-defined]
            threading.Thread(target=server.serve_forever, daemon=True, name=f"gpu-tunnel-{connection['id']}").start()
            self._tunnels[connection["id"]] = tunnel
            return tunnel

    def get(self, connection_id: str) -> SshTunnel | None:
        with self._lock:
            return self._tunnels.get(connection_id)

    def status(self, connection_id: str) -> dict[str, Any]:
        tunnel = self.get(connection_id)
        if not tunnel:
            return {"connected": False}
        return {"connected": tunnel.healthy(), "local_url": tunnel.local_url}

    def close(self, connection_id: str) -> None:
        with self._lock:
            tunnel = self._tunnels.pop(connection_id, None)
        if tunnel:
            tunnel.close()

    def training_preflight(self, connection_id: str) -> dict[str, Any]:
        tunnel = self.get(connection_id)
        if not tunnel or not tunnel.healthy():
            raise TunnelError("SSH 连接尚未建立")
        try:
            stdin, stdout, stderr = tunnel.client.exec_command(_PREFLIGHT_COMMAND, timeout=15)
            del stdin
            output = stdout.read(65536).decode("utf-8", "replace")
            error = stderr.read(4096).decode("utf-8", "replace")
            exit_code = stdout.channel.recv_exit_status()
        except Exception as exc:
            raise TunnelError(f"远程训练器预检失败: {type(exc).__name__}") from exc
        if exit_code != 0:
            raise TunnelError("远程训练器不可用；请在服务器部署 baa_remote_runner")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise TunnelError("远程训练器返回格式无效") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ready":
            raise TunnelError(str(payload.get("message") or error or "远程训练器未就绪"))
        return {
            "runner_ready": True, "runner_version": str(payload.get("version") or "unknown")[:80],
            "python": str(payload.get("python") or "unknown")[:120], "cuda": bool(payload.get("cuda")),
            "gpu_name": str(payload.get("gpu_name") or "")[:160],
        }


connection_manager = ConnectionManager()


def connection_url(connection: dict) -> str | None:
    if connection.get("connection_type") == "direct":
        return str(connection.get("base_url") or "")
    return connection_manager.status(connection["id"]).get("local_url")


def _request(connection: dict, method: str, path: str, **kwargs):
    base_url = connection_url(connection)
    if not base_url:
        raise TunnelError("连接尚未建立")
    url = base_url.rstrip("/") + path
    if connection.get("connection_type") == "ssh" or urlsplit(base_url).hostname in {"localhost", "127.0.0.1", "::1"}:
        return requests.request(method, url, timeout=kwargs.pop("timeout", 20), allow_redirects=False, **kwargs)
    return safe_http_request(method, url, timeout=kwargs.pop("timeout", 20), max_redirects=0, **kwargs)


def models_at(connection: dict) -> list[str]:
    response = _request(connection, "GET", "/v1/models", timeout=5)
    response.raise_for_status()
    payload = response.json()
    return [str(item["id"]) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]


def test_model_at(connection: dict, model: str) -> str:
    response = _request(
        connection, "POST", "/v1/chat/completions", timeout=20,
        headers={"Content-Type": "application/json", "Authorization": "Bearer no-key"},
        json={
            "model": model, "messages": [{"role": "user", "content": "Reply exactly: OK"}],
            "max_tokens": 5, "stream": False,
        },
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices") or []
    return str(((choices[0].get("message") or {}).get("content") if choices else "") or "").strip()[:200]


def set_enabled(database: Database, workspace_id: str, enabled: bool) -> bool:
    database.put(
        "gpu_settings", {"id": f"gpu_settings_{workspace_id}", "workspace_id": workspace_id, "enabled": enabled},
        workspace_id=workspace_id,
    )
    return enabled


def get_enabled(database: Database, workspace_id: str) -> bool:
    item = database.get("gpu_settings", f"gpu_settings_{workspace_id}")
    return bool(item.get("enabled", True)) if item else True


def _nvidia() -> dict:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"kind": "none", "gpus": [], "message": "未检测到 nvidia-smi：本机无 NVIDIA 独显，或驱动未安装"}
    try:
        process = subprocess.run(
            [executable, "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"kind": "none", "gpus": [], "message": f"nvidia-smi 执行失败: {type(exc).__name__}"}
    if process.returncode:
        return {"kind": "none", "gpus": [], "message": "nvidia-smi 返回非零"}
    gpus = []
    for line in process.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 4:
            def number(value: str) -> int:
                try:
                    return int(float(value.split()[0]))
                except (ValueError, IndexError):
                    return 0
            gpus.append({"name": parts[0], "memory_total_mb": number(parts[1]), "memory_used_mb": number(parts[2]), "utilization_pct": number(parts[3])})
    return {
        "kind": "nvidia" if gpus else "none", "gpus": gpus,
        "message": f"检测到 {len(gpus)} 块 NVIDIA 独显" if gpus else "nvidia-smi 无 GPU 输出",
    }


def _cuda() -> dict:
    try:
        torch = importlib.import_module("torch")
        available = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if available else 0
        return {
            "available": available, "device_count": count, "cuda_version": getattr(torch.version, "cuda", None),
            "message": f"PyTorch CUDA 可用（{count} 块设备）" if available else "PyTorch 未检测到可用 CUDA 设备",
        }
    except ImportError:
        return {"available": False, "device_count": 0, "message": "未安装 PyTorch，无法使用本机 CUDA 训练"}
    except Exception as exc:  # pragma: no cover - driver-specific failures
        return {"available": False, "device_count": 0, "message": f"CUDA 运行时探测失败: {type(exc).__name__}"}


def _ollama() -> dict:
    try:
        response = requests.get("http://127.0.0.1:11434/api/tags", timeout=0.6)
        response.raise_for_status()
        models = [item.get("name") for item in response.json().get("models", []) if item.get("name")]
        return {"online": True, "models": models, "message": f"Ollama 在线，发现 {len(models)} 个模型"}
    except Exception as exc:
        return {"online": False, "models": [], "message": f"Ollama 未运行（{type(exc).__name__}）"}


def detect_all(database: Database, workspace_id: str) -> dict:
    nvidia = _nvidia()
    all_gpus = []
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        all_gpus = [{"name": "Apple Silicon", "kind": "integrated"}]
        if nvidia["kind"] == "none":
            nvidia = {"kind": "integrated", "gpus": all_gpus, "message": "检测到 Apple Silicon 集成 GPU（不支持 CUDA）"}
    return {"gpu": nvidia, "all_gpus": all_gpus, "cuda": _cuda(), "ollama": _ollama(), "enabled": get_enabled(database, workspace_id)}
