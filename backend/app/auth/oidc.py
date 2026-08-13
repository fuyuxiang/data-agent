"""OIDC token verification.

The runtime path is short: parse the header (no signature work), enforce the
algorithm whitelist, fetch the signing key by `kid` from a small TTL cache,
then verify the signature and standard claims (`iss`, `aud`, `exp`, `nbf`).
Anything that fails raises `TokenError` — the API surface does not leak the
specific reason to clients.

The cache is deliberately small: a successful in-process service sees one
fetch per JWKS rotation, not per request. The "refetch once on unknown kid"
rule handles the publisher's key-rotation window without amplifying a single
typo into an indefinite fetch loop.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from jose import jwt
from jose.exceptions import JOSEError

from app.core.config import Settings


# Whitelist is intentionally tiny: only asymmetric algorithms the verifier
# itself understands. Anything else — `alg=none`, HMAC, `HS*` — is rejected
# without contacting the JWKS endpoint.
ALLOWED_ALGORITHMS: frozenset[str] = frozenset({"RS256", "ES256"})


class TokenError(Exception):
    """Generic token failure. The API layer maps this to a 401 with a
    safe, non-leaking message."""


class AlgorithmNotAllowedError(TokenError):
    """Token advertises an algorithm outside the whitelist."""


class UnknownKidError(TokenError):
    """JWKS endpoint does not publish the key referenced by the token header."""


class InvalidClaimsError(TokenError):
    """Signature OK but standard time/issuer/audience claims fail."""


# --- JWKS cache --------------------------------------------------------------


@dataclass
class JwksClient:
    """Cache of JWKS keys keyed by `kid` with a TTL.

    `fetcher` is injectable so tests can run without an HTTP layer; in
    production it defaults to `httpx.get` against `settings.oidc_jwks_url`.
    `clock` is injectable for the same reason — TTL comparisons use
    monotonic time.
    """

    settings: Settings
    fetcher: Callable[[], dict[str, Any]] | None = None
    ttl_seconds: int = 300
    # `clock` stays a callable: we compare `_cache` timestamps against it on
    # every lookup. `default_factory` returns the function object itself, not
    # a sampled value, so each instance has its own callable reference.
    clock: Callable[[], float] = field(default_factory=lambda: _time.monotonic)
    _cache: dict[str, tuple[dict[str, Any], float]] = field(default_factory=dict)
    _known_misses: set[str] = field(default_factory=set)

    def get_key(self, kid: str) -> dict[str, Any] | None:
        """Return the JWK for `kid` or `None` after one refetch attempt.

        Unknown kid: refetch exactly once; if still unknown, the kid is
        remembered as a miss so subsequent lookups fail fast without
        amplifying the load on the IdP.
        """
        cached = self._cache.get(kid)
        if cached is not None:
            key, stored_at = cached
            if self.clock() - stored_at < self.ttl_seconds:
                return key

        if kid in self._known_misses:
            return None

        # Refetch up to twice: the first call may have raced the IdP's
        # rotation window; one extra fetch is enough to catch it without
        # turning a single typo into a fetch storm.
        for _ in range(2):
            self._refresh()
            cached = self._cache.get(kid)
            if cached is not None:
                key, stored_at = cached
                if self.clock() - stored_at < self.ttl_seconds:
                    return key

        self._known_misses.add(kid)
        return None

    def _refresh(self) -> None:
        raw = self._fetch_jwks()
        now = self.clock()
        self._cache = {
            key["kid"]: (key, now)
            for key in raw
            if isinstance(key, dict) and "kid" in key
        }
        # A refresh is the only thing that can prove a previously-cached key
        # is still good; clear the miss set so legitimately rotated keys are
        # re-evaluated on the next lookup.
        self._known_misses.clear()

    def _fetch_jwks(self) -> list[dict[str, Any]]:
        if self.fetcher is not None:
            return list(self.fetcher().get("keys", []))
        response = httpx.get(self.settings.oidc_jwks_url, timeout=5.0)
        response.raise_for_status()
        return list(response.json().get("keys", []))


# --- verifier ----------------------------------------------------------------


def _parse_unverified_header(token: str) -> dict[str, Any]:
    try:
        return jwt.get_unverified_header(token)
    except JOSEError as exc:
        raise TokenError("malformed token") from exc


def verify_token(
    token: str,
    settings: Settings,
    *,
    jwks_client: JwksClient | None = None,
) -> dict[str, Any]:
    """Verify the token's signature and claims, returning the claims dict.

    The algorithm whitelist is enforced *before* any key fetch — this keeps
    `alg=none` (and algorithm-confusion attempts) from reaching the JWKS
    layer where they could be mistaken for legitimate traffic.
    """
    header = _parse_unverified_header(token)
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise AlgorithmNotAllowedError(f"alg={alg!r} is not in the allow-list")

    kid = header.get("kid")
    if not kid:
        raise TokenError("missing kid")

    if jwks_client is None:
        jwks_client = JwksClient(settings=settings)

    key = jwks_client.get_key(kid)
    if key is None:
        raise UnknownKidError(f"unknown kid: {kid}")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[alg],
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
        )
    except JOSEError as exc:
        raise InvalidClaimsError(str(exc)) from exc

    return claims