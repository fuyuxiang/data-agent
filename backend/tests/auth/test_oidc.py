"""OIDC token verification tests (S1 Task 1, Step 1).

Plan: identity is verified at the edge and an immutable `PrincipalContext`
flows downstream. The verifier must reject anything that isn't RS256/ES256,
enforce iss/aud/exp/nbf, refetch once on an unknown `kid`, and never sign
the same algorithm confusion bug that has bitten the field before.
"""

from __future__ import annotations

import time

import pytest

from app.auth.oidc import (
    AlgorithmNotAllowedError,
    JwksClient,
    TokenError,
    UnknownKidError,
    verify_token,
)
from app.core.config import Settings
from tests.auth.factories import (
    FakeJwksServer,
    generate_ec_keypair,
    generate_rsa_keypair,
    issue_token,
    issue_unsigned_token,
    make_jwks_fetcher,
)


# --- test settings -----------------------------------------------------------


def _settings(**overrides) -> Settings:
    """Override-only constructor: missing fields use the test defaults so the
    verifier sees a consistent issuer/audience pair across cases."""
    defaults = dict(
        environment="test",
        auth_mode="oidc",
        oidc_issuer="https://issuer.test",
        oidc_audience="data-agent",
    )
    defaults.update(overrides)
    # Bypass the cached singleton so each test gets a fresh Settings instance.
    return Settings(**defaults)


# --- happy path --------------------------------------------------------------


def test_valid_rs256_token_passes_and_claims_are_returned() -> None:
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})

    issued = issue_token(
        private_pem=private_pem,
        kid=public_jwk["kid"],
        alg="RS256",
        claims={"sub": "alice", "preferred_username": "alice"},
    )
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    claims = verify_token(issued.token, settings=_settings(), jwks_client=client)

    assert claims["sub"] == "alice"
    assert claims["preferred_username"] == "alice"
    assert claims["iss"] == "https://issuer.test"
    assert claims["aud"] == "data-agent"


# --- temporal claims ---------------------------------------------------------


def test_expired_token_is_rejected() -> None:
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})
    issued = issue_token(
        private_pem=private_pem,
        kid=public_jwk["kid"],
        claims={"exp": int(time.time()) - 60},
    )
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    with pytest.raises(TokenError):
        verify_token(issued.token, settings=_settings(), jwks_client=client)


def test_nbf_in_the_future_is_rejected() -> None:
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})
    issued = issue_token(
        private_pem=private_pem,
        kid=public_jwk["kid"],
        claims={"nbf": int(time.time()) + 3600},
    )
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    with pytest.raises(TokenError):
        verify_token(issued.token, settings=_settings(), jwks_client=client)


# --- iss / aud ---------------------------------------------------------------


def test_wrong_audience_is_rejected() -> None:
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})
    issued = issue_token(
        private_pem=private_pem,
        kid=public_jwk["kid"],
        claims={"aud": "some-other-service"},
    )
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    with pytest.raises(TokenError):
        verify_token(issued.token, settings=_settings(), jwks_client=client)


def test_wrong_issuer_is_rejected() -> None:
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})
    issued = issue_token(
        private_pem=private_pem,
        kid=public_jwk["kid"],
        claims={"iss": "https://evil.test"},
    )
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    with pytest.raises(TokenError):
        verify_token(issued.token, settings=_settings(), jwks_client=client)


# --- algorithm whitelist -----------------------------------------------------


def test_alg_none_is_rejected() -> None:
    issued = issue_unsigned_token(
        claims={
            "iss": "https://issuer.test",
            "aud": "data-agent",
            "sub": "mallory",
        },
        alg="none",
    )
    # Even if a JWKS happens to serve a kid matching the token, alg=none must
    # never reach the key-fetch step — assert it fails before any network.
    server = FakeJwksServer()
    server.fetch_count = 0  # baseline

    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))
    with pytest.raises(AlgorithmNotAllowedError):
        verify_token(issued.token, settings=_settings(), jwks_client=client)

    assert server.fetch_count == 0, "alg=none must be rejected without contacting JWKS"


def test_algorithm_confusion_with_hs256_is_rejected() -> None:
    """An attacker holding the public RSA key tries to sign HS256 using the
    public key bytes as the HMAC secret. The verifier must refuse, not
    silently accept a token signed with a different family than advertised."""
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})

    # The attacker treats the public JWK as the HMAC secret.
    import json as _json
    public_pem_like = _json.dumps(public_jwk).encode("utf-8")
    issued = issue_token(
        private_pem=public_pem_like,
        kid=public_jwk["kid"],
        alg="HS256",
        claims={"sub": "mallory"},
    )
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    with pytest.raises(AlgorithmNotAllowedError):
        verify_token(issued.token, settings=_settings(), jwks_client=client)


def test_es256_algorithm_is_accepted() -> None:
    """Whitelist is RS256/ES256; ES256 must work end-to-end, not just RS256."""
    private_pem, public_jwk = generate_ec_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})
    issued = issue_token(
        private_pem=private_pem,
        kid=public_jwk["kid"],
        alg="ES256",
        claims={"sub": "bob"},
    )
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    claims = verify_token(issued.token, settings=_settings(), jwks_client=client)

    assert claims["sub"] == "bob"


# --- JWKS refetch on unknown kid ---------------------------------------------


def test_unknown_kid_triggers_one_refetch_then_fails() -> None:
    """A kid that the JWKS server never publishes must trigger exactly one
    refetch and then fail. The verifier must not loop or retry."""
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer()  # JWKS server is empty
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    issued = issue_token(private_pem=private_pem, kid=public_jwk["kid"])

    with pytest.raises(UnknownKidError):
        verify_token(issued.token, settings=_settings(), jwks_client=client)

    # 1 initial fetch (miss) + 1 refetch (still miss) = 2 fetches total.
    assert server.fetch_count == 2


def test_jwks_cache_avoids_repeated_fetches_within_ttl() -> None:
    private_pem, public_jwk = generate_rsa_keypair()
    server = FakeJwksServer(keys={public_jwk["kid"]: public_jwk})
    client = JwksClient(settings=_settings(), fetcher=make_jwks_fetcher(server))

    for _ in range(5):
        issued = issue_token(
            private_pem=private_pem,
            kid=public_jwk["kid"],
            claims={"sub": "carol", "jti": str(_)},
        )
        verify_token(issued.token, settings=_settings(), jwks_client=client)

    # Five valid tokens, one JWKS fetch: the cache must hit on tokens 2..5.
    assert server.fetch_count == 1