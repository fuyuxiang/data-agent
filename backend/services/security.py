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
from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidURL


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


def _resolved_addresses(hostname: str, port: int, *, allow_private: bool | None = None) -> list[str]:
    try:
        addresses = sorted({item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(hostname, port)})
    except socket.gaierror as exc:
        raise ValueError("外部地址无法解析") from exc
    if not addresses:
        raise ValueError("外部地址未解析到可用 IP")
    if allow_private is None:
        allow_private = os.getenv("MERIDIAN_ALLOW_PRIVATE_NETWORK", "0") == "1"
    if not allow_private:
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ValueError("外部地址解析结果无效") from exc
            if not ip.is_global:
                raise ValueError("默认禁止访问本机、内网、链路本地或保留地址")
    return addresses


def validate_outbound_host(
    hostname: str, port: int, *, allowlist: set[str] | None = None, allow_private: bool = False,
) -> list[str]:
    normalized = str(hostname or "").strip().lower().rstrip(".")
    if not normalized or not 1 <= int(port) <= 65535:
        raise ValueError("外部主机或端口无效")
    if allowlist:
        allowed = {str(item).strip().lower().rstrip(".") for item in allowlist if str(item).strip()}
        if not any(normalized == item or normalized.endswith(f".{item}") for item in allowed):
            raise ValueError("外部主机不在服务端域名白名单中")
    return _resolved_addresses(normalized, int(port), allow_private=allow_private)


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
    _resolved_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    return parsed.geturl()


class _PinnedHTTPAdapter(HTTPAdapter):
    def __init__(self, hostname: str, address: str, port: int):
        self._hostname = hostname.lower().rstrip(".")
        self._address = address
        self._port = port
        super().__init__(max_retries=0)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        requested_host = str(host_params.get("host") or "").lower().rstrip(".")
        if requested_host != self._hostname:
            raise InvalidURL("外部请求主机与已验证目标不一致")
        host_params["host"] = self._address
        if host_params.get("scheme") == "https":
            pool_kwargs["assert_hostname"] = self._hostname
            pool_kwargs["server_hostname"] = self._hostname
        return host_params, pool_kwargs

    def add_headers(self, request, **kwargs):
        del kwargs
        default_port = 443 if urlsplit(request.url).scheme == "https" else 80
        request.headers["Host"] = self._hostname if self._port == default_port else f"{self._hostname}:{self._port}"


def _pinned_session(url: str) -> requests.Session:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    address = _resolved_addresses(str(parsed.hostname), port)[0]
    session = requests.Session()
    # Environment proxies resolve the destination themselves and would defeat
    # the DNS pinning performed here. Outbound proxying must be configured at
    # the network layer instead of through HTTP(S)_PROXY.
    session.trust_env = False
    adapter = _PinnedHTTPAdapter(str(parsed.hostname), address, port)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_http_request(
    method: str, url: str, *, timeout: int = 20, max_redirects: int = 3,
    max_response_bytes: int = 20 * 1024 * 1024, **kwargs,
) -> requests.Response:
    current = validate_outbound_url(url)
    caller_stream = bool(kwargs.pop("stream", False))
    for redirect in range(max_redirects + 1):
        session = _pinned_session(current)
        response = session.request(
            method, current, timeout=timeout, allow_redirects=False, stream=True, **kwargs,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > max_response_bytes:
                response.close()
                session.close()
                raise ValueError("外部响应超过大小限制")
            if not caller_stream:
                chunks, size = [], 0
                for chunk in response.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > max_response_bytes:
                        response.close()
                        session.close()
                        raise ValueError("外部响应超过大小限制")
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
                session.close()
            else:
                response._meridian_session = session
            return response
        if redirect >= max_redirects:
            response.close()
            session.close()
            raise ValueError("外部请求重定向次数过多")
        location = response.headers.get("Location")
        if not location:
            response._meridian_session = session
            return response
        response.close()
        session.close()
        current = validate_outbound_url(urljoin(current, location))
    raise ValueError("外部请求无法完成")
