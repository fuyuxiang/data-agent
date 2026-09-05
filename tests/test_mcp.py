from __future__ import annotations

import sys


def test_persistent_stdio_mcp_discovery_call_and_validation(client, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_SECRET_KEY", "must-not-reach-mcp")
    server_script = tmp_path / "mcp_server.py"
    server_script.write_text(
        """
import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-mcp", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "echo", "description": "echo a message",
            "inputSchema": {
                "type": "object", "properties": {"message": {"type": "string"}},
                "required": ["message"], "additionalProperties": False,
            },
        }]}
    elif method == "tools/call":
        text = f"{os.getpid()}:{message['params']['arguments']['message']}:{os.getenv('MERIDIAN_SECRET_KEY', 'missing')}"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "本地测试 MCP", "transport": "stdio", "command": sys.executable,
            "args": [str(server_script)],
        },
    )
    assert created.status_code == 201
    server_id = created.get_json()["item"]["id"]
    connected = client.post(f"/api/mcp/servers/{server_id}/test")
    assert connected.status_code == 200, connected.get_json()
    assert connected.get_json()["result"]["tools"][0]["name"] == "echo"

    first = client.post(
        f"/api/mcp/servers/{server_id}/tools/echo/call", json={"arguments": {"message": "one"}},
    )
    second = client.post(
        f"/api/mcp/servers/{server_id}/tools/echo/call", json={"arguments": {"message": "two"}},
    )
    assert first.status_code == second.status_code == 200
    first_text = first.get_json()["result"]["content"][0]["text"]
    second_text = second.get_json()["result"]["content"][0]["text"]
    assert first_text.split(":", 1)[0] == second_text.split(":", 1)[0]
    assert second_text.endswith(":two:missing")

    invalid = client.post(f"/api/mcp/servers/{server_id}/tools/echo/call", json={"arguments": {}})
    assert invalid.status_code == 400
    assert "必填参数" in invalid.get_json()["error"]
    assert client.delete(f"/api/mcp/servers/{server_id}").status_code == 200
