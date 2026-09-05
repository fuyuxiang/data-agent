from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from flask import Flask, current_app, has_app_context

from ..core.database import Database, utcnow
from .security import SecretVault, validate_outbound_url


ALLOWED_STDIO_COMMANDS = frozenset({"uvx", "uv", "npx", "npm", "node", "python", "python3", "deno"})
PROTOCOL_VERSION = "2025-06-18"


def _safe_command(command: str) -> str:
    testing = has_app_context() and bool(current_app.config.get("TESTING"))
    if not testing and os.getenv("MERIDIAN_ENABLE_STDIO_MCP", "0") != "1":
        raise PermissionError("stdio MCP 默认禁用；仅可由系统所有者在受信主机上显式启用")
    basename = Path(command).stem.lower()
    if basename not in ALLOWED_STDIO_COMMANDS:
        raise ValueError(f"MCP stdio 命令不在白名单中：{command}")
    if os.path.sep in command or (os.altsep and os.altsep in command):
        resolved = command
    else:
        resolved = shutil.which(command) or ""
    if not resolved:
        raise FileNotFoundError(f"找不到 MCP 命令：{command}")
    return resolved


def validate_tool_arguments(schema: dict, arguments: dict, path: str = "参数") -> None:
    if not isinstance(arguments, dict):
        raise ValueError(f"{path}必须是对象")
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    for key in required:
        if key not in arguments:
            raise ValueError(f"缺少必填参数：{key}")
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ValueError(f"存在未声明参数：{', '.join(unknown)}")
    type_map: dict[str, Any] = {
        "string": str, "number": (int, float), "integer": int,
        "boolean": bool, "array": list, "object": dict,
    }
    for key, value in arguments.items():
        rule = properties.get(key)
        if not isinstance(rule, dict):
            continue
        expected = rule.get("type")
        if expected in type_map:
            if expected in {"number", "integer"} and isinstance(value, bool):
                raise ValueError(f"参数 {key} 类型错误：期望 {expected}")
            if not isinstance(value, type_map[expected]):
                raise ValueError(f"参数 {key} 类型错误：期望 {expected}")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"参数 {key} 不在允许值中")


class Transport(ABC):
    @abstractmethod
    async def connect(self) -> dict: ...

    @abstractmethod
    async def request(self, method: str, params: dict) -> Any: ...

    @abstractmethod
    async def close(self) -> None: ...


class StdioTransport(Transport):
    def __init__(self, command: str, arguments: list[str], environment: dict[str, str]):
        self.command = _safe_command(command)
        self.arguments = [str(item) for item in arguments]
        if any(item in {"-c", "-e", "--eval", "--print"} for item in self.arguments):
            raise ValueError("stdio MCP 禁止解释器内联执行参数")
        self.environment = {**os.environ, **{str(key): str(value) for key, value in environment.items()}}
        self.process: asyncio.subprocess.Process | None = None
        self.request_id = 0
        self.lock = asyncio.Lock()

    async def connect(self) -> dict:
        self.process = await asyncio.create_subprocess_exec(
            self.command, *self.arguments,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self.environment,
        )
        result = await self.request("initialize", _initialize_params())
        await self._notification("notifications/initialized", {})
        return result if isinstance(result, dict) else {}

    async def _notification(self, method: str, params: dict) -> None:
        if not self.process or not self.process.stdin:
            raise ConnectionError("MCP stdio 进程未运行")
        message = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False) + "\n"
        self.process.stdin.write(message.encode())
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict) -> Any:
        async with self.lock:
            if not self.process or not self.process.stdin or not self.process.stdout:
                raise ConnectionError("MCP stdio 进程未运行")
            self.request_id += 1
            request_id = self.request_id
            message = json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                ensure_ascii=False,
            ) + "\n"
            self.process.stdin.write(message.encode())
            await self.process.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=30)
                if not line:
                    stderr = ""
                    if self.process.stderr:
                        try:
                            stderr = (await asyncio.wait_for(self.process.stderr.read(), timeout=0.2)).decode(
                                "utf-8", errors="replace",
                            )[-1000:]
                        except (asyncio.TimeoutError, RuntimeError):
                            pass
                    raise ConnectionError(f"MCP stdio 进程已关闭：{stderr}")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(response, dict) or response.get("id") != request_id:
                    continue
                if response.get("error"):
                    error = response["error"]
                    raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
                return response.get("result")

    async def close(self) -> None:
        if not self.process:
            return
        try:
            if self.process.stdin:
                self.process.stdin.close()
            await asyncio.wait_for(self.process.wait(), timeout=3)
        except (asyncio.TimeoutError, ProcessLookupError):
            self.process.kill()
            await self.process.wait()
        self.process = None


class HttpTransport(Transport):
    def __init__(self, url: str, headers: dict[str, str], *, legacy_sse: bool = False):
        self.url = url
        self.headers = headers
        self.legacy_sse = legacy_sse
        self.endpoint = url
        self.client = None
        self.request_id = 0
        self.lock = asyncio.Lock()
        self.session_id = ""

    async def connect(self) -> dict:
        import httpx

        self.client = httpx.AsyncClient(
            headers={"Accept": "application/json, text/event-stream", **self.headers}, timeout=30,
        )
        if self.legacy_sse:
            async with self.client.stream("GET", self.url) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    try:
                        value = json.loads(payload)
                        endpoint = value.get("endpoint") if isinstance(value, dict) else None
                    except json.JSONDecodeError:
                        endpoint = payload
                    if endpoint:
                        self.endpoint = validate_outbound_url(urljoin(self.url, str(endpoint)))
                        break
        initialized = await self.request("initialize", _initialize_params())
        await self._notification("notifications/initialized", {})
        return initialized if isinstance(initialized, dict) else {}

    def _response_data(self, response, request_id: int) -> Any:
        content_type = response.headers.get("content-type", "")
        if response.status_code == 202 or not response.content:
            return {}
        if "text/event-stream" in content_type:
            candidates = []
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    value = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    candidates.append(value)
            data = next((item for item in candidates if item.get("id") == request_id), candidates[-1] if candidates else {})
        else:
            data = response.json()
        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
        return data.get("result") if isinstance(data, dict) and "result" in data else data

    async def _notification(self, method: str, params: dict) -> None:
        if not self.client:
            raise ConnectionError("MCP HTTP 客户端未连接")
        headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
        response = await self.client.post(
            self.endpoint, json={"jsonrpc": "2.0", "method": method, "params": params}, headers=headers,
        )
        response.raise_for_status()

    async def request(self, method: str, params: dict) -> Any:
        async with self.lock:
            if not self.client:
                raise ConnectionError("MCP HTTP 客户端未连接")
            self.request_id += 1
            request_id = self.request_id
            headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
            response = await self.client.post(
                self.endpoint,
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                headers=headers, timeout=60,
            )
            response.raise_for_status()
            returned_session = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
            if returned_session:
                self.session_id = returned_session
            return self._response_data(response, request_id)

    async def close(self) -> None:
        if self.client:
            if self.session_id:
                try:
                    await self.client.delete(self.endpoint, headers={"Mcp-Session-Id": self.session_id})
                except Exception:
                    pass
            await self.client.aclose()
            self.client = None


def _initialize_params() -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "meridian-analytics-workbench", "version": "1.0.0"},
    }


@dataclass
class MCPConnection:
    server: dict
    secrets: dict
    status: str = "disconnected"
    last_error: str = ""
    tools: list[dict] = field(default_factory=list)
    server_info: dict = field(default_factory=dict)
    transport: Transport | None = None
    reconnect_attempts: int = 0

    def build_transport(self) -> Transport:
        transport = self.server.get("transport")
        if transport == "stdio":
            return StdioTransport(
                str(self.server.get("command") or ""), list(self.server.get("args") or []),
                self.secrets.get("env", {}) if isinstance(self.secrets, dict) else {},
            )
        headers = self.secrets.get("headers", {}) if isinstance(self.secrets, dict) else {}
        return HttpTransport(
            validate_outbound_url(str(self.server.get("url") or "")),
            headers, legacy_sse=transport == "sse",
        )

    async def connect(self) -> bool:
        if self.status == "connected" and self.transport:
            return True
        self.status, self.last_error = "connecting", ""
        try:
            self.transport = self.build_transport()
            self.server_info = await self.transport.connect()
            listed = await self.transport.request("tools/list", {})
            self.tools = listed.get("tools", []) if isinstance(listed, dict) else []
            self.status, self.reconnect_attempts = "connected", 0
            return True
        except Exception as exc:
            self.status, self.last_error = "error", str(exc)
            if self.transport:
                await self.transport.close()
            self.transport = None
            return False

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        if self.status != "connected" or not self.transport:
            if not await self.connect():
                raise ConnectionError(self.last_error or "MCP 连接失败")
        tool = next((item for item in self.tools if item.get("name") == tool_name), None)
        if not tool:
            raise FileNotFoundError(f"MCP 工具不存在：{tool_name}")
        validate_tool_arguments(tool.get("inputSchema") or {}, arguments)
        try:
            result = await self.transport.request("tools/call", {"name": tool_name, "arguments": arguments})
        except (ConnectionError, OSError, RuntimeError):
            if self.reconnect_attempts >= 3:
                raise
            self.reconnect_attempts += 1
            if self.transport:
                await self.transport.close()
            self.transport = None
            self.status = "disconnected"
            await asyncio.sleep(2 ** (self.reconnect_attempts - 1))
            if not await self.connect() or not self.transport:
                raise ConnectionError(self.last_error or "MCP 重连失败")
            result = await self.transport.request("tools/call", {"name": tool_name, "arguments": arguments})
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content") or []
            detail = " ".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
            raise RuntimeError(detail or "MCP 工具返回错误")
        return result

    async def close(self) -> None:
        if self.transport:
            await self.transport.close()
        self.transport = None
        self.status = "disconnected"


class MCPManager:
    def __init__(self, app: Flask):
        self.app = app
        self.database: Database = app.extensions["meridian_db"]
        self.secret_key = app.config["VAULT_KEY"]
        self.connections: dict[str, MCPConnection] = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="meridian-mcp", daemon=True)
        self.lock = threading.RLock()
        self.thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coroutine, timeout: int = 70):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=timeout)

    def _connection(self, server: dict) -> MCPConnection:
        with self.lock:
            existing = self.connections.get(server["id"])
            fingerprint = json.dumps(
                {key: server.get(key) for key in ("transport", "url", "command", "args", "credential")},
                sort_keys=True, default=str,
            )
            if existing and existing.server.get("_fingerprint") == fingerprint:
                return existing
            if existing:
                self._submit(existing.close(), timeout=10)
            value = dict(server)
            value["_fingerprint"] = fingerprint
            secrets = SecretVault(self.secret_key).open(server.get("credential", ""), {})
            connection = MCPConnection(value, secrets if isinstance(secrets, dict) else {})
            self.connections[server["id"]] = connection
            return connection

    def connect_server(self, server: dict) -> dict:
        connection = self._connection(server)
        connection.reconnect_attempts = 0
        self._submit(connection.connect())
        state = self.status(server["id"])
        updated = {
            "status": state["status"], "last_error": state["last_error"],
            "tools": connection.tools, "server_info": connection.server_info,
        }
        if state["status"] == "connected":
            updated["last_connected_at"] = utcnow()
        self.database.patch("mcp_servers", server["id"], updated)
        return state

    def call_tool(self, server: dict, tool_name: str, arguments: dict) -> Any:
        connection = self._connection(server)
        result = self._submit(connection.call_tool(tool_name, arguments))
        self.database.patch(
            "mcp_servers", server["id"],
            {
                "status": connection.status, "last_error": connection.last_error,
                "tools": connection.tools, "last_used_at": utcnow(),
            },
        )
        return result

    def status(self, server_id: str) -> dict:
        connection = self.connections.get(server_id)
        if not connection:
            return {"server_id": server_id, "status": "disconnected", "last_error": "", "tool_count": 0, "tools": []}
        return {
            "server_id": server_id, "status": connection.status, "last_error": connection.last_error,
            "tool_count": len(connection.tools), "tools": [item.get("name") for item in connection.tools],
            "server_info": connection.server_info,
        }

    def remove_server(self, server_id: str) -> None:
        with self.lock:
            connection = self.connections.pop(server_id, None)
        if connection:
            self._submit(connection.close(), timeout=10)


def get_mcp_manager(app: Flask | None = None) -> MCPManager:
    app = app or current_app._get_current_object()
    manager = app.extensions.get("meridian_mcp")
    if manager is None:
        manager = MCPManager(app)
        app.extensions["meridian_mcp"] = manager
    return manager
