"""Unit tests for mcp_server/auth.py.

Uses a locally-generated RSA keypair and real PyJWT encode/decode — no
network calls, no dependency on a live OIDC provider. TokenVerifier's JWKS
lookup is monkeypatched to return the local test key directly, since the
thing under test is the *verification logic* (signature/iss/aud/exp/scope),
not PyJWKClient's HTTP fetching (that's PyJWT's own tested code).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.auth import (
    AuthConfigError,
    AuthError,
    BearerAuthASGIMiddleware,
    OIDCAuthConfig,
    TokenVerifier,
)

ISSUER = "https://test-idp.example.com/"
AUDIENCE = "netcortex-mcp-rca"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_token(private_key, *, issuer=ISSUER, audience=AUDIENCE, scope=None,
                 expires_in=300, not_before_offset=0):
    now = int(time.time())
    claims = {"iss": issuer, "aud": audience, "iat": now, "exp": now + expires_in,
              "nbf": now + not_before_offset, "sub": "user-123"}
    if scope is not None:
        claims["scope"] = scope
    return jwt.encode(claims, private_key, algorithm="RS256")


def _verifier_with_fixed_key(public_key, issuer=ISSUER, audience=AUDIENCE, required_scope=None):
    config = OIDCAuthConfig(
        enabled=True, issuer=issuer.rstrip("/"), audience=audience,
        jwks_uri="https://test-idp.example.com/.well-known/jwks.json",
        required_scope=required_scope,
    )
    verifier = TokenVerifier(config)
    # Bypass real JWKS HTTP fetch — return a fake jwk_client resolving to our
    # local test public key regardless of `kid`.
    verifier._get_jwk_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key)
    )
    return verifier


# ---------------------------------------------------------------------------
# OIDCAuthConfig
# ---------------------------------------------------------------------------

def test_config_disabled_by_default_requires_nothing():
    config = OIDCAuthConfig.from_cfg({})
    assert config.enabled is False


def test_config_enabled_without_required_keys_raises():
    with pytest.raises(AuthConfigError):
        OIDCAuthConfig.from_cfg({"mcp_server": {"auth": {"enabled": True}}})


def test_config_enabled_with_all_keys_succeeds():
    config = OIDCAuthConfig.from_cfg({"mcp_server": {"auth": {
        "enabled": True, "issuer": ISSUER, "audience": AUDIENCE,
        "jwks_uri": "https://x/.well-known/jwks.json",
    }}})
    assert config.enabled is True
    assert config.issuer == ISSUER.rstrip("/")


# ---------------------------------------------------------------------------
# TokenVerifier
# ---------------------------------------------------------------------------

def test_valid_token_is_accepted(keypair):
    private_key, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key)
    token = _make_token(private_key)
    claims = verifier.verify(f"Bearer {token}")
    assert claims["sub"] == "user-123"


def test_missing_header_rejected(keypair):
    _, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key)
    with pytest.raises(AuthError):
        verifier.verify(None)


def test_malformed_header_rejected(keypair):
    _, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key)
    with pytest.raises(AuthError):
        verifier.verify("NotBearer sometoken")
    with pytest.raises(AuthError):
        verifier.verify("Bearer")  # no token after scheme


def test_expired_token_rejected(keypair):
    private_key, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key)
    token = _make_token(private_key, expires_in=-60)
    with pytest.raises(AuthError, match="expired"):
        verifier.verify(f"Bearer {token}")


def test_wrong_audience_rejected(keypair):
    private_key, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key)
    token = _make_token(private_key, audience="some-other-service")
    with pytest.raises(AuthError, match="audience"):
        verifier.verify(f"Bearer {token}")


def test_wrong_issuer_rejected(keypair):
    private_key, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key)
    token = _make_token(private_key, issuer="https://not-our-idp.example.com/")
    with pytest.raises(AuthError, match="issuer"):
        verifier.verify(f"Bearer {token}")


def test_required_scope_enforced(keypair):
    private_key, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key, required_scope="netcortex:run_rca")

    token_without_scope = _make_token(private_key, scope="netcortex:read")
    with pytest.raises(AuthError, match="scope"):
        verifier.verify(f"Bearer {token_without_scope}")

    token_with_scope = _make_token(private_key, scope="netcortex:read netcortex:run_rca")
    claims = verifier.verify(f"Bearer {token_with_scope}")
    assert "netcortex:run_rca" in claims["scope"]


def test_tampered_signature_rejected(keypair):
    private_key, public_key = keypair
    verifier = _verifier_with_fixed_key(public_key)
    token = _make_token(private_key)
    tampered = token[:-4] + ("A" if token[-4] != "A" else "B") + token[-3:]
    with pytest.raises(AuthError):
        verifier.verify(f"Bearer {tampered}")


# ---------------------------------------------------------------------------
# BearerAuthASGIMiddleware — ASGI-level pass/reject behavior
# ---------------------------------------------------------------------------

class _RecordingApp:
    def __init__(self):
        self.called_with_scope = None

    async def __call__(self, scope, receive, send):
        self.called_with_scope = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _run_asgi(app, path="/mcp", headers=None):
    scope = {"type": "http", "path": path, "headers": [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]}
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_middleware_disabled_passes_through_unconditionally():
    inner = _RecordingApp()
    config = OIDCAuthConfig(enabled=False)
    middleware = BearerAuthASGIMiddleware(inner, config)
    sent = await _run_asgi(middleware)
    assert inner.called_with_scope is not None
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_middleware_health_path_allowlisted(keypair):
    _, public_key = keypair
    inner = _RecordingApp()
    config = OIDCAuthConfig(
        enabled=True, issuer=ISSUER.rstrip("/"), audience=AUDIENCE,
        jwks_uri="https://x/.well-known/jwks.json",
    )
    middleware = BearerAuthASGIMiddleware(inner, config)
    sent = await _run_asgi(middleware, path="/health")  # no Authorization header
    assert inner.called_with_scope is not None
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_middleware_rejects_missing_token_on_protected_path():
    inner = _RecordingApp()
    config = OIDCAuthConfig(
        enabled=True, issuer=ISSUER.rstrip("/"), audience=AUDIENCE,
        jwks_uri="https://x/.well-known/jwks.json",
    )
    middleware = BearerAuthASGIMiddleware(inner, config)
    sent = await _run_asgi(middleware, path="/mcp")
    assert inner.called_with_scope is None  # inner app never invoked
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_middleware_accepts_valid_token_and_forwards_claims(keypair):
    private_key, public_key = keypair
    inner = _RecordingApp()
    config = OIDCAuthConfig(
        enabled=True, issuer=ISSUER.rstrip("/"), audience=AUDIENCE,
        jwks_uri="https://x/.well-known/jwks.json",
    )
    middleware = BearerAuthASGIMiddleware(inner, config)
    middleware._verifier = _verifier_with_fixed_key(public_key)  # inject fixed key, skip real JWKS

    token = _make_token(private_key)
    sent = await _run_asgi(middleware, path="/mcp", headers={"Authorization": f"Bearer {token}"})

    assert sent[0]["status"] == 200
    assert inner.called_with_scope["state"]["auth_subject"] == "user-123"
