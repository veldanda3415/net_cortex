"""OAuth 2.1 / OIDC bearer-token auth for the MCP RCA server (2026-07-28 spec).

The spec expects MCP servers acting as OAuth *resource servers*: every request
carries `Authorization: Bearer <JWT>`, and the server validates that token
against its issuing OIDC provider rather than minting or storing sessions
itself. This module implements exactly that — nothing more:

  - Fetches and caches the issuer's JWKS (refreshed on a TTL, and once
    on-demand if a token references an unknown `kid`, to tolerate key
    rotation without a restart).
  - Verifies signature, `iss`, `aud`, `exp`/`nbf`, and — if configured —
    that the token carries a required scope.
  - Applies as ASGI middleware in front of the Streamable HTTP app, so it
    covers `tasks/send`-equivalent tool calls, `tasks/get` polling, and the
    `server/discover` endpoint uniformly. Auth is a transport-layer concern;
    it deliberately does not know about `run_rca` / `get_report` / etc.

Design choices worth calling out:
  - Fails closed. If `mcp_server.auth.enabled: true` and verification can't
    be performed (JWKS unreachable, no verification library installed),
    requests are rejected — the server does not silently fall back to
    unauthenticated mode.
  - No token introspection endpoint support (offline JWT verification only).
    Add one if your IdP issues opaque tokens instead of JWTs.
  - `/health` is allow-listed unauthenticated by default so k8s liveness/
    readiness probes don't need credentials; everything else requires a
    valid token when auth is enabled.
  - Issuer comparison is done manually (not via PyJWT's built-in `issuer=`
    exact-match) after stripping trailing slashes on both sides, because
    real IdPs are inconsistent about whether `iss` carries a trailing slash
    (Auth0 includes it, many others don't) — see verify() below.

This module has no import-time dependency on the `mcp` SDK's internal
transport classes — it only needs an ASGI `app` to wrap — so it will keep
working even if the SDK's exact Starlette wiring changes between versions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger("net_cortex.mcp_server.auth")

try:
    import jwt as pyjwt  # PyJWT
    from jwt import PyJWKClient
    _HAS_JWT = True
except ImportError:  # pragma: no cover - exercised via test that patches _HAS_JWT
    pyjwt = None  # type: ignore
    PyJWKClient = None  # type: ignore
    _HAS_JWT = False


class AuthConfigError(RuntimeError):
    """Raised at startup when auth is enabled but misconfigured."""


class AuthError(RuntimeError):
    """Raised per-request when a token is missing, malformed, or invalid."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class OIDCAuthConfig:
    enabled: bool = False
    issuer: str = ""
    audience: str = ""
    jwks_uri: str = ""
    required_scope: str | None = None
    jwks_cache_seconds: int = 300
    leeway_seconds: int = 30
    jwks_fetch_timeout_seconds: int = 10
    allowed_unauthenticated_paths: tuple[str, ...] = ("/health", "/healthz")

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "OIDCAuthConfig":
        auth_cfg = (cfg.get("mcp_server", {}) or {}).get("auth", {}) or {}
        enabled = bool(auth_cfg.get("enabled", False))
        instance = cls(
            enabled=enabled,
            issuer=str(auth_cfg.get("issuer", "")).rstrip("/"),
            audience=str(auth_cfg.get("audience", "")),
            jwks_uri=str(auth_cfg.get("jwks_uri", "")),
            required_scope=auth_cfg.get("required_scope") or None,
            jwks_cache_seconds=int(auth_cfg.get("jwks_cache_seconds", 300)),
            leeway_seconds=int(auth_cfg.get("leeway_seconds", 30)),
            jwks_fetch_timeout_seconds=int(auth_cfg.get("jwks_fetch_timeout_seconds", 10)),
        )
        if enabled:
            missing = [k for k in ("issuer", "audience", "jwks_uri") if not getattr(instance, k)]
            if missing:
                raise AuthConfigError(
                    f"mcp_server.auth.enabled=true but missing required keys: {missing}. "
                    "Set mcp_server.auth.{issuer,audience,jwks_uri} in config.yaml."
                )
            if not _HAS_JWT:
                raise AuthConfigError(
                    "mcp_server.auth.enabled=true but PyJWT is not installed. "
                    "Run: pip install 'pyjwt[crypto]'"
                )
            jwks_parsed = urlparse(instance.jwks_uri)
            if jwks_parsed.scheme != "https" and jwks_parsed.hostname not in {"localhost", "127.0.0.1"}:
                raise AuthConfigError(
                    f"mcp_server.auth.jwks_uri must use https:// (got scheme={jwks_parsed.scheme!r}); "
                    "a plain http:// JWKS endpoint is a MITM vector for signing keys. "
                    "http is only permitted for localhost/127.0.0.1 during local development."
                )
        return instance


class TokenVerifier:
    """Verifies bearer JWTs against a cached JWKS. Fails closed on any error."""

    def __init__(self, config: OIDCAuthConfig) -> None:
        self._config = config
        self._jwk_client: Any | None = None
        self._jwk_client_created_at: float = 0.0

    def _get_jwk_client(self) -> Any:
        now = time.monotonic()
        if (
            self._jwk_client is None
            or (now - self._jwk_client_created_at) > self._config.jwks_cache_seconds
        ):
            # PyJWKClient does its own internal caching of the fetched key set;
            # recreating it periodically forces a refresh so rotated signing
            # keys (new `kid`) are picked up without a server restart.
            self._jwk_client = PyJWKClient(
                self._config.jwks_uri,
                cache_keys=True,
                timeout=self._config.jwks_fetch_timeout_seconds,
            )
            self._jwk_client_created_at = now
        return self._jwk_client

    def verify(self, authorization_header: str | None) -> dict[str, Any]:
        """Verify the Authorization header. Returns decoded claims or raises AuthError."""
        if not authorization_header:
            raise AuthError("Missing Authorization header")

        parts = authorization_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthError("Authorization header must be 'Bearer <token>'")
        token = parts[1].strip()
        if not token:
            raise AuthError("Empty bearer token")

        try:
            jwk_client = self._get_jwk_client()
            signing_key = jwk_client.get_signing_key_from_jwt(token)
        except Exception as exc:
            logger.warning("JWKS/signing-key lookup failed: %s", exc)
            raise AuthError("Unable to resolve signing key for token", status_code=401) from exc

        try:
            # Issuer is checked manually below (see module docstring) rather
            # than via PyJWT's built-in `issuer=` exact-match, because that
            # match is strict-string and real IdPs are inconsistent about
            # trailing slashes on `iss`.
            claims = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._config.audience,
                leeway=self._config.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud"], "verify_iss": False},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise AuthError("Token expired", status_code=401) from exc
        except pyjwt.InvalidAudienceError as exc:
            raise AuthError("Token audience does not match this server", status_code=401) from exc
        except pyjwt.PyJWTError as exc:
            raise AuthError(f"Token validation failed: {exc}", status_code=401) from exc

        token_issuer = str(claims.get("iss", "")).rstrip("/")
        if token_issuer != self._config.issuer:
            raise AuthError("Token issuer not trusted", status_code=401)

        if self._config.required_scope:
            scopes = set(str(claims.get("scope", "")).split())
            if self._config.required_scope not in scopes:
                raise AuthError(
                    f"Token missing required scope '{self._config.required_scope}'",
                    status_code=403,
                )

        return claims


class BearerAuthASGIMiddleware:
    """Pure-ASGI middleware: verifies bearer tokens in front of any ASGI app.

    Written against the raw ASGI interface (not a specific web framework) so
    it wraps whatever Starlette app the installed `mcp` SDK version produces
    for Streamable HTTP, without a hard import-time dependency on Starlette.
    """

    def __init__(self, app: Callable, config: OIDCAuthConfig) -> None:
        self._app = app
        self._config = config
        self._verifier = TokenVerifier(config) if config.enabled else None

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not self._config.enabled:
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._config.allowed_unauthenticated_paths:
            await self._app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth_header = headers.get("authorization")

        try:
            # verify() does synchronous, potentially slow network I/O (JWKS
            # fetch on cache miss / unknown kid / rotation) — run it off the
            # event loop thread so a cold start or hung JWKS endpoint can't
            # stall every other request this server is handling.
            claims = await asyncio.to_thread(self._verifier.verify, auth_header)  # type: ignore[union-attr]
        except AuthError as exc:
            await self._send_error(send, exc.status_code, str(exc))
            logger.info("MCP request rejected path=%s reason=%s", path, exc)
            return

        # Make claims available to downstream tool handlers via ASGI scope,
        # mirroring how request-scoped auth context is typically threaded
        # through ASGI stacks (e.g. Starlette's `request.state`).
        scope.setdefault("state", {})
        scope["state"]["auth_claims"] = claims
        scope["state"]["auth_subject"] = claims.get("sub")

        await self._app(scope, receive, send)

    @staticmethod
    async def _send_error(send: Callable, status_code: int, message: str) -> None:
        body = json.dumps({"error": "unauthorized", "detail": message}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="netcortex-mcp"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def load_auth_config(cfg: dict[str, Any]) -> OIDCAuthConfig:
    """Convenience wrapper — raises AuthConfigError at startup on bad config."""
    return OIDCAuthConfig.from_cfg(cfg)
