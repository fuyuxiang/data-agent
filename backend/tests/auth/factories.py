"""Test-only factories for OIDC verification tests.

The runtime code must never read filesystem or shell out — this module gives
the tests local RSA/EC keypairs, an `issue_token` helper and a controllable
JWKS endpoint stand-in, so the production verifier can be exercised without
talking to any real identity provider.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jose import jwk, jwt
from jose.utils import long_to_base64


# --- key material ------------------------------------------------------------


def generate_rsa_keypair(key_size: int = 2048) -> tuple[bytes, bytes]:
    """Return (private_pem, public_jwk_dict) for an RSA keypair.

    The public material is returned as a JWK dict so callers can wire it
    directly into the fake JWKS endpoint without re-encoding.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()

    def _int_to_base64url(value: int) -> str:
        byte_length = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")

    public_jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": _kid(),
        "n": _int_to_base64url(public_numbers.n),
        "e": _int_to_base64url(public_numbers.e),
    }
    return private_pem, public_jwk


def generate_ec_keypair() -> tuple[bytes, dict[str, Any]]:
    """Return (private_pem, public_jwk_dict) for an EC P-256 keypair."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()
    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "use": "sig",
        "alg": "ES256",
        "kid": _kid(),
        "x": long_to_base64(public_numbers.x).decode("ascii"),
        "y": long_to_base64(public_numbers.y).decode("ascii"),
    }
    return private_pem, public_jwk


def _kid() -> str:
    return uuid.uuid4().hex


# --- token issuance ----------------------------------------------------------


@dataclass
class IssuedToken:
    token: str
    kid: str
    alg: str


def issue_token(
    *,
    private_pem: bytes,
    kid: str,
    alg: str = "RS256",
    claims: dict[str, Any] | None = None,
    headers_overrides: dict[str, Any] | None = None,
    headers_extras: dict[str, Any] | None = None,
) -> IssuedToken:
    """Sign a JWT with the given key.

    `headers_overrides` replaces any auto-derived header values (used to
    inject `alg=none`); `headers_extras` adds fields without overriding.
    """
    claims = dict(claims or {})
    claims.setdefault("iss", "https://issuer.test")
    claims.setdefault("aud", "data-agent")
    claims.setdefault("sub", "user-123")
    claims.setdefault("exp", int(time.time()) + 300)
    claims.setdefault("iat", int(time.time()))
    claims.setdefault("nbf", int(time.time()) - 1)

    headers: dict[str, Any] = {"kid": kid}
    if headers_extras:
        headers.update(headers_extras)
    if headers_overrides:
        headers.update(headers_overrides)

    token = jwt.encode(claims, private_pem, algorithm=alg, headers=headers)
    return IssuedToken(token=token, kid=kid, alg=alg)


def issue_unsigned_token(
    *, claims: dict[str, Any], kid: str = "unsigned", alg: str | None = None
) -> IssuedToken:
    """Build a token whose signature segment is empty — used to assert that
    the verifier rejects `alg=none` regardless of the rest of the token."""
    header: dict[str, Any] = {"kid": kid, "alg": alg}
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode("utf-8")).rstrip(b"=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=")
    token = (header_b64 + b"." + payload_b64 + b".").decode("ascii")
    return IssuedToken(token=token, kid=kid, alg=alg or "none")


# --- fake JWKS server ---------------------------------------------------------


@dataclass
class FakeJwksServer:
    """In-memory JWKS endpoint, exposing only the keys it currently holds.

    `set_keys` replaces the published set; `fetch_count` is the running
    request tally — tests assert cache and re-fetch behaviour from it.
    """

    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetch_count: int = 0

    def set_keys(self, keys: dict[str, dict[str, Any]]) -> None:
        self.keys = dict(keys)

    def add_key(self, key_jwk: dict[str, Any]) -> None:
        self.keys[key_jwk["kid"]] = key_jwk

    def fetch(self) -> dict[str, Any]:
        self.fetch_count += 1
        return {"keys": [dict(value) for value in self.keys.values()]}


def make_jwks_fetcher(server: FakeJwksServer) -> Callable[[], dict[str, Any]]:
    return server.fetch


def jwk_for_keyserver(public_jwk: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a public JWK through python-jose so the verifier can consume it."""
    return jwk.construct(public_jwk).to_dict()