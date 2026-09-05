from __future__ import annotations

import json

def test_gpu_switch_and_detection_contract(client, monkeypatch):
    monkeypatch.setattr(
        "backend.services.remote_gpu._nvidia",
        lambda: {"kind": "nvidia", "gpus": [{"name": "RTX Test"}], "message": "ready"},
    )
    monkeypatch.setattr(
        "backend.services.remote_gpu._cuda",
        lambda: {"available": True, "device_count": 1, "message": "ready"},
    )
    monkeypatch.setattr(
        "backend.services.remote_gpu._ollama",
        lambda: {"online": True, "models": ["qwen-test"], "message": "ready"},
    )
    assert client.post("/api/gpu/enabled", json={"enabled": False}).get_json() == {
        "ok": True, "enabled": False,
    }
    status = client.get("/api/gpu/status").get_json()
    assert status["enabled"] is False
    assert status["gpu"]["gpus"][0]["name"] == "RTX Test"
    assert status["cuda"]["available"] is True
    assert status["ollama"]["models"] == ["qwen-test"]
    assert client.post("/api/gpu/enabled", json={"enabled": "yes"}).status_code == 400


def test_direct_connection_model_lifecycle(client, monkeypatch):
    assert client.post(
        "/api/gpu/connections",
        json={"name": "unsafe", "connection_type": "direct", "base_url": "http://example.com"},
    ).status_code == 400
    created = client.post(
        "/api/gpu/connections",
        json={"name": "本地 Ollama", "connection_type": "direct", "base_url": "http://127.0.0.1:11434"},
    )
    assert created.status_code == 201
    connection = created.get_json()["connection"]
    connection_id = connection["id"]

    monkeypatch.setattr("backend.api.gpu.models_at", lambda _item: ["qwen2.5:7b", "deepseek-r1:8b"])
    connected = client.post(f"/api/gpu/connections/{connection_id}/connect").get_json()
    assert connected["connected"] is True
    models = client.get(f"/api/gpu/connections/{connection_id}/models").get_json()["models"]
    assert models == ["qwen2.5:7b", "deepseek-r1:8b"]

    registered = client.post(
        f"/api/gpu/connections/{connection_id}/models/register", json={"model": models[0]},
    ).get_json()
    assert registered["provider"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert registered["provider"]["has_api_key"] is True

    monkeypatch.setattr("backend.api.gpu.test_model_at", lambda _item, model: f"OK:{model}")
    tested = client.post(
        f"/api/gpu/connections/{connection_id}/models/test", json={"model": models[0]},
    ).get_json()
    assert tested["reply"] == "OK:qwen2.5:7b"
    assert client.delete(f"/api/gpu/connections/{connection_id}").status_code == 200
    assert client.get(f"/api/gpu/connections/{connection_id}/status").status_code == 404


def test_ssh_fingerprint_gate_encrypted_password_and_runner_preflight(app, client, monkeypatch):
    created = client.post(
        "/api/gpu/connections",
        json={
            "name": "GPU 节点", "connection_type": "ssh", "host": "gpu.example.com", "port": 2222,
            "username": "analyst", "target_host": "127.0.0.1", "target_port": 8000,
            "auth_method": "password", "password": "never-plaintext",
        },
    )
    assert created.status_code == 201
    connection = created.get_json()["connection"]
    assert connection["has_password"] is True
    assert "credential" not in connection
    assert "never-plaintext" not in json.dumps(client.get("/api/gpu/connections").get_json())

    # No credential is sent before an explicitly trusted fingerprint exists.
    gated = client.post(f"/api/gpu/connections/{connection['id']}/connect")
    assert gated.status_code == 400
    assert "指纹" in gated.get_json()["message"]

    class FakeKey:
        @staticmethod
        def get_name():
            return "ssh-ed25519"

        @staticmethod
        def get_base64():
            return "dGVzdC1wdWJsaWMta2V5"

        @staticmethod
        def asbytes():
            return b"test-public-key"

    key = FakeKey()
    inspected = {"type": key.get_name(), "fingerprint": "SHA256:shown", "base64": key.get_base64()}
    monkeypatch.setattr("backend.api.gpu.inspect_host_key", lambda _connection: inspected)
    monkeypatch.setattr("backend.services.remote_gpu._decode_key", lambda _type, _base64: key)
    assert client.post(f"/api/gpu/connections/{connection['id']}/host-key").get_json()["host_key"] == inspected
    trusted = client.post(
        f"/api/gpu/connections/{connection['id']}/trust-host-key",
        json={"key_type": key.get_name(), "key_base64": key.get_base64()},
    )
    assert trusted.status_code == 200
    assert trusted.get_json()["fingerprint"].startswith("SHA256:")

    captured = {}

    class FakeTunnel:
        local_url = "http://127.0.0.1:43123"

    def fake_connect(item, host_key, password):
        captured.update({"item": item, "host_key": host_key, "password": password})
        return FakeTunnel()

    monkeypatch.setattr("backend.api.gpu.connection_manager.connect", fake_connect)
    linked = client.post(f"/api/gpu/connections/{connection['id']}/connect").get_json()
    assert linked["local_url"] == FakeTunnel.local_url
    assert captured["password"] == "never-plaintext"
    assert captured["host_key"]["fingerprint"].startswith("SHA256:")

    runner = {
        "runner_ready": True, "runner_version": "0.1", "python": "3.12",
        "cuda": True, "gpu_name": "NVIDIA Test",
    }
    monkeypatch.setattr("backend.api.gpu.connection_manager.training_preflight", lambda _id: runner)
    checked = client.post(
        f"/api/gpu/connections/{connection['id']}/training/preflight"
    ).get_json()["training_runner"]
    assert checked == runner
    with app.app_context():
        stored = app.extensions["meridian_db"].get("gpu_connections", connection["id"])
        assert stored["training_runner"]["runner_ready"] is True
        assert "never-plaintext" not in json.dumps(stored)
