from __future__ import annotations

from types import SimpleNamespace

import httpx
from openai import APIConnectionError


def test_provider_connection_error_is_actionable(app, client, monkeypatch):
    provider_id = "provider-connection-error"
    app.extensions["meridian_db"].put(
        "providers",
        {
            "id": provider_id,
            "workspace_id": "default",
            "name": "测试模型",
            "base_url": "https://model.example.test/v1",
            "model": "example-model",
        },
        workspace_id="default",
    )

    class Completions:
        def create(self, **_kwargs):
            raise APIConnectionError(
                request=httpx.Request("POST", "https://model.example.test/v1/chat/completions"),
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(
        "backend.services.models.resolve_provider",
        lambda _provider_id, _workspace_id=None: (
            {"model": "example-model", "base_url": "https://model.example.test/v1"},
            fake_client,
        ),
    )

    response = client.post(f"/api/providers/{provider_id}/test")

    assert response.status_code == 502
    assert "检查网络、代理和 Base URL" in response.get_json()["error"]


def test_provider_resolution_ignores_broken_system_proxy_by_default(app, monkeypatch):
    from backend.services.models import resolve_provider
    from backend.services.security import SecretVault

    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("MERIDIAN_MODEL_TRUST_ENV_PROXY", raising=False)
    monkeypatch.setattr("backend.services.models.OpenAI", fake_openai)
    with app.app_context():
        credential = SecretVault(app.config["VAULT_KEY"]).seal({"api_key": "sk-test"})
        app.extensions["meridian_db"].put(
            "providers",
            {
                "id": "provider-direct-network",
                "workspace_id": "default",
                "name": "直连模型",
                "base_url": "https://api.openai.com/v1",
                "model": "example-model",
                "credential": credential,
            },
            workspace_id="default",
        )
        provider, client = resolve_provider("provider-direct-network", "default")

    assert provider["model"] == "example-model"
    assert client is not None
    assert getattr(captured["http_client"], "_trust_env") is False


def test_provider_resolution_can_opt_into_system_proxy(app, monkeypatch):
    from backend.services.models import resolve_provider
    from backend.services.security import SecretVault

    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("MERIDIAN_MODEL_TRUST_ENV_PROXY", "1")
    monkeypatch.setattr("backend.services.models.OpenAI", fake_openai)
    with app.app_context():
        credential = SecretVault(app.config["VAULT_KEY"]).seal({"api_key": "sk-test"})
        app.extensions["meridian_db"].put(
            "providers",
            {
                "id": "provider-proxy-network",
                "workspace_id": "default",
                "name": "代理模型",
                "base_url": "https://api.openai.com/v1",
                "model": "example-model",
                "credential": credential,
            },
            workspace_id="default",
        )
        provider, client = resolve_provider("provider-proxy-network", "default")

    assert provider["model"] == "example-model"
    assert client is not None
    assert getattr(captured["http_client"], "_trust_env") is True
