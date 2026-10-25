"""MCP endpoint client — one per configured telemetry endpoint.

Speaks telemetry contract v2 to a telemetry MCP server and owns that
endpoint's reliability controls:

  * a reused MCP client session (opened lazily, kept for the process,
    reopened after a transport failure) instead of a new thread + event loop
    + connection per call;
  * a per-attempt timeout;
  * bounded retry with full jitter on transport errors only;
  * a circuit breaker that stops calling an endpoint after N consecutive
    transport failures and half-opens after T seconds.

Errors are raised, not swallowed: the registry (providers/factory.py) turns
them into a `failed` coverage entry for this endpoint, which is how a
finding ends up marked degraded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from pydantic import ValidationError

from contract import CONTRACT_VERSION, Signal, TelemetryQuery, ToolResponse, check_contract_version
from providers import resilience
from providers.resilience import CircuitBreaker, RetryPolicy, is_transport_error

logger = logging.getLogger("net_cortex.adapter.mcp")

# Tools for the built-in domains; the catalog (domains.json `tool`) is the
# source of truth at runtime and may route any domain to get_signals.
DOMAIN_TOOLS: dict[str, str] = {
    "metrics": "get_metrics",
    "logs": "get_logs",
    "routing": "get_routing_events",
    "config": "get_config_changes",
}
GENERIC_TOOL = "get_signals"

INPROC_SCHEME = "inproc://"


class ToolCallError(RuntimeError):
    """The endpoint was reachable but the tool call itself failed."""


class EndpointUnavailable(RuntimeError):
    """Every attempt to reach the endpoint failed at the transport level."""


@dataclass(frozen=True)
class EndpointConfig:
    name: str
    url: str
    timeout_seconds: float = 8.0
    auth: Literal["none", "bearer_env", "oidc"] = "none"
    token_env: str | None = None
    required: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    failure_threshold: int = 3
    reset_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ConfigValidationError: telemetry endpoint needs a name")
        if not self.url:
            raise ValueError(f"ConfigValidationError: telemetry endpoint '{self.name}' needs a url")
        if self.timeout_seconds <= 0:
            raise ValueError(f"ConfigValidationError: endpoint '{self.name}' timeout_seconds must be > 0")
        if self.auth not in ("none", "bearer_env", "oidc"):
            raise ValueError(
                f"ConfigValidationError: endpoint '{self.name}' auth must be none | bearer_env | oidc, got {self.auth!r}"
            )
        if self.auth == "bearer_env" and not self.token_env:
            raise ValueError(f"ConfigValidationError: endpoint '{self.name}' auth=bearer_env needs token_env")
        if self.auth == "oidc":
            # OAuth is deferred for the local-cluster deployment (see CLAUDE.md);
            # refuse at startup rather than silently calling unauthenticated.
            raise ValueError(
                f"ConfigValidationError: endpoint '{self.name}' auth=oidc is not implemented yet; "
                "use bearer_env or none"
            )


def parse_tool_response(payload: dict[str, Any]) -> tuple[ToolResponse, int]:
    """Validate a v2 ToolResponse, skipping individually malformed signals.

    Returns the response and how many signals were dropped, so one bad row
    from a source degrades coverage instead of discarding the whole reply.
    """
    if not isinstance(payload, dict):
        raise ToolCallError(f"tool returned {type(payload).__name__}, expected a JSON object")
    check_contract_version(str(payload.get("contract_version", "")))
    raw_signals = payload.get("signals") or []
    good: list[Signal] = []
    skipped = 0
    for row in raw_signals:
        try:
            good.append(Signal.model_validate(row))
        except ValidationError as exc:
            skipped += 1
            logger.warning("Skipping malformed signal: %s", exc.errors()[:1])
    try:
        response = ToolResponse.model_validate({**payload, "signals": []})
    except ValidationError as exc:
        raise ToolCallError(f"malformed ToolResponse envelope: {exc}") from exc
    response.signals = good
    return response, skipped


def _result_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False):
        text = " ".join(getattr(c, "text", "") for c in result.content or [])
        raise ToolCallError(text or "tool call failed")
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    try:
        return json.loads(result.content[0].text)
    except (IndexError, AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise ToolCallError(f"unparseable tool result: {exc}") from exc


def _describe(exc: BaseException) -> str:
    # anyio wraps transport failures in ExceptionGroups; report the leaf.
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError):
        return "timeout"
    return f"{type(exc).__name__}: {exc}"[:300]


ClientFactory = Callable[[], AbstractAsyncContextManager[Any]]

# Strong references to every live session owner task (asyncio only keeps weak
# ones), with the event that stops it, so shutdown can close them all.
_SESSION_TASKS: dict[asyncio.Task, asyncio.Event] = {}


async def close_all_sessions() -> None:
    """Close every endpoint session opened on the running loop (shutdown/tests)."""
    loop = asyncio.get_running_loop()
    tasks = [t for t in _SESSION_TASKS if t.get_loop() is loop]
    for task in tasks:
        _SESSION_TASKS[task].set()
    for task in tasks:
        with suppress(BaseException):
            await asyncio.wait_for(task, timeout=2.0)


class _SessionOwner:
    """Keeps one MCP client session open per endpoint and event loop.

    The session is entered and exited inside a dedicated owner task because
    the SDK's transports use anyio task groups, which must be exited from the
    task that entered them. Callers on any task share the session; requests
    are multiplexed by JSON-RPC id.
    """

    def __init__(self, factory: ClientFactory, name: str) -> None:
        self._factory = factory
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self.opened = 0  # sessions opened so far (observability / tests)

    def _bind_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A session belongs to the loop that opened it; one from a dead
            # loop (e.g. a previous test) can't be closed from here, only dropped.
            self._loop, self._lock = loop, asyncio.Lock()
            self._client = self._task = self._stop = None
        assert self._lock is not None
        return self._lock

    async def get(self) -> Any:
        async with self._bind_loop():
            if self._client is not None and self._task is not None and not self._task.done():
                return self._client
            loop = asyncio.get_running_loop()
            ready: asyncio.Future = loop.create_future()
            stop = asyncio.Event()
            task = loop.create_task(self._own(ready, stop), name=f"mcp-session-{self._name}")
            _SESSION_TASKS[task] = stop
            task.add_done_callback(lambda t: _SESSION_TASKS.pop(t, None))
            try:
                client = await ready
            except BaseException:
                stop.set()
                task.cancel()
                raise
            self._client, self._task, self._stop = client, task, stop
            self.opened += 1
            return client

    async def _own(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        try:
            async with self._factory() as client:
                if not ready.done():
                    ready.set_result(client)
                await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.debug("MCP session closed endpoint=%s error=%r", self._name, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def reset(self) -> None:
        if self._loop is not asyncio.get_running_loop():
            return
        task, stop = self._task, self._stop
        self._client = self._task = self._stop = None
        if stop is not None:
            stop.set()
        if task is not None:
            with suppress(BaseException):
                await asyncio.wait_for(task, timeout=2.0)

    aclose = reset


class MCPEndpointClient:
    def __init__(self, config: EndpointConfig, client_factory: ClientFactory | None = None,
                 breaker: CircuitBreaker | None = None) -> None:
        self.config = config
        self.breaker = breaker or CircuitBreaker(config.failure_threshold, config.reset_timeout_seconds)
        self._sessions = _SessionOwner(client_factory or self._default_factory(), config.name)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def required(self) -> bool:
        return self.config.required

    @property
    def sessions_opened(self) -> int:
        return self._sessions.opened

    def _default_factory(self) -> ClientFactory:
        cfg = self.config
        if cfg.url.startswith(INPROC_SCHEME):
            from providers.inproc import resolve_inproc_server

            def inproc() -> AbstractAsyncContextManager[Any]:
                return Client(resolve_inproc_server(cfg.url), read_timeout_seconds=cfg.timeout_seconds)
            return inproc

        if cfg.auth == "bearer_env":
            @asynccontextmanager
            async def bearer() -> AsyncIterator[Any]:
                token = os.environ.get(cfg.token_env or "", "")
                if not token:
                    raise ToolCallError(f"endpoint '{cfg.name}': env var {cfg.token_env} is not set")
                async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
                    async with Client(streamable_http_client(cfg.url, http_client=http),
                                      read_timeout_seconds=cfg.timeout_seconds) as client:
                        yield client
            return bearer

        return lambda: Client(cfg.url, read_timeout_seconds=cfg.timeout_seconds)

    async def _call_once(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = await self._sessions.get()
        return _result_payload(await client.call_tool(tool, arguments))

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        is_trial = self.breaker.before_call()
        settled = False
        try:
            attempt = 0
            while True:
                attempt += 1
                try:
                    payload = await asyncio.wait_for(self._call_once(tool, arguments), self.config.timeout_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not is_transport_error(exc):
                        # Reachable and answering: endpoint is healthy as far as the breaker cares.
                        self.breaker.record_success()
                        settled = True
                        raise
                    await self._sessions.reset()
                    if attempt < self.config.retry.max_attempts:
                        delay = self.config.retry.delay(attempt)
                        logger.info("Retrying endpoint=%s tool=%s attempt=%s delay=%.2fs error=%r",
                                    self.name, tool, attempt + 1, delay, exc)
                        await resilience.sleep(delay)
                        continue
                    self.breaker.record_failure()
                    settled = True
                    raise EndpointUnavailable(f"{_describe(exc)} after {attempt} attempt(s)") from exc
                self.breaker.record_success()
                settled = True
                return payload
        finally:
            if is_trial and not settled:
                self.breaker.release_trial()

    async def fetch(self, domain: str, query: TelemetryQuery, tool: str | None = None) -> tuple[ToolResponse, int]:
        tool = tool or DOMAIN_TOOLS.get(domain, GENERIC_TOOL)
        arguments = {**query.model_dump(mode="json", exclude_none=True), "contract_version": CONTRACT_VERSION}
        if tool == GENERIC_TOOL:
            arguments["domain"] = domain
        return parse_tool_response(await self.call_tool(tool, arguments))

    async def aclose(self) -> None:
        await self._sessions.aclose()
