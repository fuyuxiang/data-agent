from __future__ import annotations

import os
import time
from urllib.parse import urlsplit

from flask import current_app
from openai import OpenAI

from ..core.database import Database
from .security import SecretVault, mask_secret, validate_outbound_url


def _db() -> Database:
    return current_app.extensions["meridian_db"]


def public_provider(provider: dict) -> dict:
    value = dict(provider)
    vault = SecretVault(current_app.config["VAULT_KEY"])
    secret = vault.open(value.pop("credential", ""), {})
    key = secret.get("api_key", "") if isinstance(secret, dict) else ""
    value["has_api_key"] = bool(key or (value.get("secret_source") == "environment" and os.getenv("OPENAI_API_KEY")))
    value["masked_api_key"] = mask_secret(key or os.getenv("OPENAI_API_KEY", ""))
    return value


def _provider_url(value: str, *, allow_loopback: bool = False) -> str:
    hostname = urlsplit(value).hostname
    if allow_loopback and hostname in {"localhost", "127.0.0.1", "::1"}:
        return value
    return validate_outbound_url(value)


def save_provider(
    payload: dict, provider_id: str | None = None, *, allow_loopback: bool = False,
) -> dict:
    provider_id = provider_id or _db().new_id("mdl")
    current = _db().get("providers", provider_id) or {}
    api_key = str(payload.get("api_key") or "").strip()
    credential = current.get("credential", "")
    if api_key:
        credential = SecretVault(current_app.config["VAULT_KEY"]).seal({"api_key": api_key})
    base_url = str(
        payload.get("base_url") or current.get("base_url") or "https://api.openai.com/v1",
    ).rstrip("/")
    if base_url:
        base_url = _provider_url(
            base_url,
            allow_loopback=allow_loopback or bool(current.get("allow_loopback")),
        )
    record = _db().put(
        "providers",
        {
            **current,
            "id": provider_id,
            "name": str(payload.get("name") or current.get("name") or "模型服务"),
            "base_url": base_url,
            "allow_loopback": bool(
                (allow_loopback or current.get("allow_loopback"))
                and urlsplit(base_url).hostname in {"localhost", "127.0.0.1", "::1"}
            ),
            "model": str(payload.get("model") or current.get("model") or "gpt-4.1-mini"),
            "protocol": str(
                payload.get("protocol") or current.get("protocol") or "chat_completions"
            ).lower().replace("-", "_"),
            "temperature": float(payload.get("temperature", current.get("temperature", 0.2))),
            "context_window": int(payload.get("context_window") or current.get("context_window") or 0) or None,
            "max_output_tokens": int(payload.get("max_output_tokens") or current.get("max_output_tokens") or 0) or None,
            "enable_thinking": bool(payload.get("enable_thinking", current.get("enable_thinking", False))),
            "reasoning_effort": str(
                payload.get("reasoning_effort") or current.get("reasoning_effort") or "medium"
            )[:32],
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
    provider_id: str | None = None, workspace_id: str = "default",
) -> tuple[dict, OpenAI] | tuple[None, None]:
    provider = _db().get("providers", provider_id) if provider_id else None
    if provider and provider["id"] != "environment-default":
        if provider.get("workspace_id", "default") != workspace_id:
            return None, None
    if provider_id and not provider:
        return None, None
    if not provider:
        providers = [
            item for item in _db().list("providers")
            if item.get("enabled", True)
            and (
                item["id"] == "environment-default"
                or item.get("workspace_id", "default") == workspace_id
            )
        ]
        provider = providers[0] if providers else None
    if not provider:
        return None, None
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(provider.get("credential", ""), {})
    api_key = (secret or {}).get("api_key") or os.getenv("OPENAI_API_KEY")
    base_url = provider.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = provider.get("model") or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    if not api_key:
        return provider | {"model": model}, None
    base_url = _provider_url(base_url, allow_loopback=bool(provider.get("allow_loopback")))
    return provider | {"model": model, "base_url": base_url}, OpenAI(api_key=api_key, base_url=base_url, timeout=60)


def test_provider(provider_id: str, workspace_id: str | None = None) -> dict:
    provider, client = resolve_provider(provider_id, workspace_id)
    if not provider:
        raise ValueError("模型配置不存在")
    if not client:
        raise ValueError("模型配置没有可用的 API Key")
    from .usage import ensure_quota, record_usage, response_usage

    effective_workspace = workspace_id or "default"
    ensure_quota(_db(), effective_workspace)
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=provider["model"],
        messages=[{"role": "user", "content": "Reply with OK only."}],
        temperature=0,
        max_tokens=8,
    )
    record_usage(
        _db(), effective_workspace, response_usage(response, provider["model"]),
        operation="provider_test",
    )
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "model": response.model,
        "reply": response.choices[0].message.content,
    }
