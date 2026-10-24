"""Retry and circuit-breaker primitives for RCA -> telemetry endpoint calls.

Both are per-endpoint and deliberately know nothing about incidents: the
breaker tracks endpoint health (shared across all RCA tasks, which is the
point — stop hammering a dead endpoint), while degradation of a particular
incident's data is derived from that request's own coverage.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import anyio
import httpx
import httpx2
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,  # asyncio.TimeoutError is an alias since 3.11
    ConnectionError,
    OSError,
    EOFError,
    httpx.TransportError,
    httpx2.TransportError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)
_TRANSIENT_HTTP_STATUS = {429, 502, 503, 504}


def is_transport_error(exc: BaseException) -> bool:
    """True for failures to reach/talk to the endpoint (worth retrying), as
    opposed to the endpoint answering with an error (not worth retrying)."""
    if isinstance(exc, BaseExceptionGroup):
        return any(is_transport_error(e) for e in exc.exceptions)
    if isinstance(exc, _TRANSPORT_EXCEPTIONS):
        return True
    if isinstance(exc, (httpx.HTTPStatusError, httpx2.HTTPStatusError)):
        return exc.response.status_code in _TRANSIENT_HTTP_STATUS
    if isinstance(exc, MCPError):
        return exc.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT)
    return False


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    base_delay_seconds: float = 0.2
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("ConfigValidationError: retry.max_attempts must be >= 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("ConfigValidationError: retry delays must be >= 0")

    def delay(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Full-jitter exponential backoff before retry number `attempt` (1-based)."""
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))
        return ceiling * rng()


class CircuitOpenError(RuntimeError):
    """Raised instead of calling an endpoint whose breaker is open."""


class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures; after
    `reset_timeout_seconds` lets exactly one trial call through (half-open).
    A successful trial closes it, a failed one re-opens it for another
    full timeout."""

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("ConfigValidationError: circuit_breaker.failure_threshold must be >= 1")
        if reset_timeout_seconds <= 0:
            raise ValueError("ConfigValidationError: circuit_breaker.reset_timeout_seconds must be > 0")
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        if self._opened_at is None:
            return "closed"
        if self._trial_in_flight or self._clock() - self._opened_at >= self.reset_timeout_seconds:
            return "half_open"
        return "open"

    def before_call(self) -> bool:
        """Raise CircuitOpenError if the call must not proceed. Returns True
        when this call is the half-open trial."""
        state = self.state
        if state == "closed":
            return False
        if state == "half_open" and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        retry_in = max(0.0, self.reset_timeout_seconds - (self._clock() - (self._opened_at or 0.0)))
        raise CircuitOpenError(f"circuit open after {self._failures} consecutive failures; retry in {retry_in:.1f}s")

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._trial_in_flight or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()
        self._trial_in_flight = False

    def release_trial(self) -> None:
        """Give up a half-open trial without a verdict (e.g. caller cancelled)."""
        self._trial_in_flight = False


async def sleep(seconds: float) -> None:
    # Indirection so tests can patch out real backoff sleeps.
    await asyncio.sleep(seconds)
