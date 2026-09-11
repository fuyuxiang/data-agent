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
