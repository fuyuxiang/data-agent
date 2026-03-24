"""
OpenAI-compatible endpoint helpers.
"""
from __future__ import annotations


def ensure_v1_path(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/v1"):
        return url
    return f"{url}/v1"


def build_v1_endpoint(base_url: str, path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return ensure_v1_path(base_url) + normalized_path
