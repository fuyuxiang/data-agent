from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

from cryptography.fernet import Fernet, InvalidToken
import requests


class SecretVault:
    """Encrypt credentials at rest with an application-local master secret."""

    def __init__(self, secret_key: str):
        digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def seal(self, value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(encoded).decode("ascii")

    def open(self, token: str, default: Any = None) -> Any:
        if not token:
            return default
        try:
            raw = self._fernet.decrypt(token.encode("ascii"))
            return json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
            return default


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) < 8:
        return "••••••"
    return f"{value[:3]}••••{value[-3:]}"


def validate_outbound_url(url: str) -> str:
    parsed = urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise ValueError("外部地址不能包含用户名或密码")
    configured_hosts = {
        item.strip().lower().rstrip(".")
        for item in os.getenv("MERIDIAN_OUTBOUND_HOST_ALLOWLIST", "").split(",")
        if item.strip()
    }
    hostname = parsed.hostname.lower().rstrip(".")
    if configured_hosts and not any(hostname == item or hostname.endswith(f".{item}") for item in configured_hosts):
        raise ValueError("外部地址不在服务端域名白名单中")
    if os.getenv("MERIDIAN_ALLOW_PRIVATE_NETWORK", "0") == "1":
        return parsed.geturl()
    addresses = []
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror:
        # Connection will still fail normally; literal/private names are checked below.
        addresses = {parsed.hostname}
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            if address.lower() in {"localhost", "localhost.localdomain"}:
                raise ValueError("默认禁止访问本机或内网地址")
            continue
        if not ip.is_global:
            raise ValueError("默认禁止访问本机、内网、链路本地或保留地址")
    return parsed.geturl()


def safe_http_request(
    method: str, url: str, *, timeout: int = 20, max_redirects: int = 3,
    max_response_bytes: int = 20 * 1024 * 1024, **kwargs,
) -> requests.Response:
    current = validate_outbound_url(url)
    caller_stream = bool(kwargs.pop("stream", False))
    for redirect in range(max_redirects + 1):
        response = requests.request(
            method, current, timeout=timeout, allow_redirects=False, stream=True, **kwargs,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > max_response_bytes:
                response.close()
                raise ValueError("外部响应超过大小限制")
            if not caller_stream:
                chunks, size = [], 0
                for chunk in response.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > max_response_bytes:
                        response.close()
                        raise ValueError("外部响应超过大小限制")
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
            return response
        if redirect >= max_redirects:
            response.close()
            raise ValueError("外部请求重定向次数过多")
        location = response.headers.get("Location")
        if not location:
            return response
        response.close()
        current = validate_outbound_url(urljoin(current, location))
    raise ValueError("外部请求无法完成")
