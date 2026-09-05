from __future__ import annotations

import os
import time

from flask import current_app
from openai import OpenAI

from ..core.database import Database
from .security import SecretVault, mask_secret


def _db() -> Database:
    return current_app.extensions["meridian_db"]


def public_provider(provider: dict) -> dict:
    value = dict(provider)
    vault = SecretVault(current_app.config["SECRET_KEY"])
    secret = vault.open(value.pop("credential", ""), {})
    key = secret.get("api_key", "") if isinstance(secret, dict) else ""
    value["has_api_key"] = bool(key or (value.get("secret_source") == "environment" and os.getenv("OPENAI_API_KEY")))
    value["masked_api_key"] = mask_secret(key or os.getenv("OPENAI_API_KEY", ""))
    return value


def save_provider(payload: dict, provider_id: str | None = None) -> dict:
    provider_id = provider_id or _db().new_id("mdl")
    current = _db().get("providers", provider_id) or {}
    api_key = str(payload.get("api_key") or "").strip()
    credential = current.get("credential", "")
    if api_key:
        credential = SecretVault(current_app.config["SECRET_KEY"]).seal({"api_key": api_key})
    record = _db().put(
        "providers",
        {
            **current,
            "id": provider_id,
            "name": str(payload.get("name") or current.get("name") or "模型服务"),
            "base_url": str(payload.get("base_url") or current.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
            "model": str(payload.get("model") or current.get("model") or "gpt-4.1-mini"),
            "temperature": float(payload.get("temperature", current.get("temperature", 0.2))),
            "context_window": int(payload.get("context_window") or current.get("context_window") or 0) or None,
            "max_output_tokens": int(payload.get("max_output_tokens") or current.get("max_output_tokens") or 0) or None,
            "enable_thinking": bool(payload.get("enable_thinking", current.get("enable_thinking", False))),
            "thinking_budget": int(payload.get("thinking_budget") or current.get("thinking_budget") or 8000),
            "input_price_per_million": payload.get("input_price_per_million", current.get("input_price_per_million")),
            "output_price_per_million": payload.get("output_price_per_million", current.get("output_price_per_million")),
            "enabled": bool(payload.get("enabled", current.get("enabled", True))),
            "secret_source": "vault" if credential else str(payload.get("secret_source") or current.get("secret_source") or "environment"),
            "credential": credential,
            "workspace_id": str(payload.get("workspace_id") or current.get("workspace_id") or "default"),
        },
        workspace_id=str(payload.get("workspace_id") or current.get("workspace_id") or "default"),
    )
    return public_provider(record)


def resolve_provider(
    provider_id: str | None = None, workspace_id: str | None = None,
) -> tuple[dict, OpenAI] | tuple[None, None]:
    provider = _db().get("providers", provider_id) if provider_id else None
    if provider and provider["id"] != "environment-default" and workspace_id is not None:
        if provider.get("workspace_id", "default") != workspace_id:
            provider = None
    if not provider:
        providers = [
            item for item in _db().list("providers")
            if item.get("enabled", True)
            and (
                item["id"] == "environment-default" or workspace_id is None
                or item.get("workspace_id", "default") == workspace_id
            )
        ]
        provider = providers[0] if providers else None
    if not provider:
        return None, None
    secret = SecretVault(current_app.config["SECRET_KEY"]).open(provider.get("credential", ""), {})
    api_key = (secret or {}).get("api_key") or os.getenv("OPENAI_API_KEY")
    base_url = provider.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = provider.get("model") or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    if not api_key:
        return provider | {"model": model}, None
    return provider | {"model": model, "base_url": base_url}, OpenAI(api_key=api_key, base_url=base_url, timeout=60)


def test_provider(provider_id: str, workspace_id: str | None = None) -> dict:
    provider, client = resolve_provider(provider_id, workspace_id)
    if not provider:
        raise ValueError("模型配置不存在")
    if not client:
        raise ValueError("模型配置没有可用的 API Key")
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=provider["model"],
        messages=[{"role": "user", "content": "Reply with OK only."}],
        temperature=0,
        max_tokens=8,
    )
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "model": response.model,
        "reply": response.choices[0].message.content,
    }
