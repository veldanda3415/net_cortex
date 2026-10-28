"""Telemetry endpoint registry — builds the RCA-side TelemetryProvider from config.

The RCA pod speaks only MCP. Each domain lists one or more telemetry MCP
endpoints; adding a second source for a domain is a config change:

  telemetry:
    defaults:                       # optional, applied to every endpoint
      timeout_seconds: 8
      retry: {max_attempts: 2, base_delay_seconds: 0.2, max_delay_seconds: 2.0}
      circuit_breaker: {failure_threshold: 3, reset_timeout_seconds: 30}
    inproc_sources:                 # sources for inproc:// endpoints (no cloud plugins)
      simulation: {enabled: true}
    domains:
      metrics:
        endpoints:
          - name: local
            url: "inproc://local"   # or http://telemetry:9001/mcp
            timeout_seconds: 8
            auth: none              # none | bearer_env (+ token_env) | oidc (not yet)
            required: true
    topology:                       # optional; defaults to the first domain's endpoints
      endpoints: [...]

A fetch fans out to every endpoint of the domain concurrently, merges their
signals (deduplicated on (source, evidence_ref)) and returns one coverage
entry per endpoint. Configs without a `telemetry:` section fall back to the
pre-v2 `providers:`/`mcp_endpoints:` keys, then to in-process simulation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from contract import CONTRACT_VERSION, Coverage, Entity, Signal, TelemetryQuery, ToolResponse, TopologyEdge, TopologyResponse
from contract.catalog_loader import Catalog, get_catalog
from providers.adapters.mcp_adapter import INPROC_SCHEME, EndpointConfig, MCPEndpointClient
from providers.base import TelemetryProvider
from providers.inproc import DEFAULT_INPROC_URL, ensure_inproc_server
from providers.resilience import RetryPolicy

logger = logging.getLogger("net_cortex.providers.factory")

RECORD_DIR_ENV = "NETCORTEX_RECORD_DIR"

_LEGACY_DOMAIN_KEYS = {"metrics": "metrics", "logs": "logs", "routing": "routing", "config": "config"}


def _endpoint_config(raw: dict[str, Any], defaults: dict[str, Any]) -> EndpointConfig:
    merged = {**defaults, **raw}
    retry = {**defaults.get("retry", {}), **raw.get("retry", {})}
    breaker = {**defaults.get("circuit_breaker", {}), **raw.get("circuit_breaker", {})}
    return EndpointConfig(
        name=str(merged.get("name", "")),
        url=str(merged.get("url", "")),
        timeout_seconds=float(merged.get("timeout_seconds", 8.0)),
        auth=str(merged.get("auth", "none")),  # type: ignore[arg-type]
        token_env=merged.get("token_env"),
        required=bool(merged.get("required", True)),
        retry=RetryPolicy(
            max_attempts=int(retry.get("max_attempts", 2)),
            base_delay_seconds=float(retry.get("base_delay_seconds", 0.2)),
            max_delay_seconds=float(retry.get("max_delay_seconds", 2.0)),
        ),
        failure_threshold=int(breaker.get("failure_threshold", 3)),
        reset_timeout_seconds=float(breaker.get("reset_timeout_seconds", 30.0)),
    )


def _legacy_telemetry_section(cfg: dict[str, Any]) -> dict[str, Any]:
    """Translate pre-v2 `providers: {<domain>: simulation|mcp}` + `mcp_endpoints`."""
    modes = cfg.get("providers", {}) or {}
    legacy_eps = cfg.get("mcp_endpoints", {}) or {}
    domains: dict[str, Any] = {}
    for domain, legacy_key in _LEGACY_DOMAIN_KEYS.items():
        mode = str(modes.get(legacy_key, "simulation")).lower()
        if mode == "simulation":
            domains[domain] = {"endpoints": [{"name": "local", "url": DEFAULT_INPROC_URL}]}
        elif mode == "mcp":
            ep = legacy_eps.get(legacy_key, {}) or {}
            if not ep.get("url"):
                raise ValueError(f"ConfigValidationError: mcp_endpoints.{legacy_key}.url must be set when providers.{legacy_key}='mcp'")
            logger.warning("Config uses deprecated providers/mcp_endpoints keys; migrate to telemetry.domains.%s.endpoints", domain)
            domains[domain] = {"endpoints": [{"name": "telemetry", "url": ep["url"],
                                              "timeout_seconds": ep.get("timeout_seconds", 8)}]}
        else:
            raise ValueError(f"ConfigValidationError: providers.{legacy_key} must be 'simulation' or 'mcp', got '{mode}'")
    return {"domains": domains}


def telemetry_section(cfg: dict[str, Any]) -> dict[str, Any]:
    section = cfg.get("telemetry")
    return section if section else _legacy_telemetry_section(cfg)


def parse_endpoints(cfg: dict[str, Any], catalog: Catalog | None = None) -> dict[str, list[EndpointConfig]]:
    """Endpoints per telemetry domain. The domain list comes from the
    detection catalog (domains.json `telemetry_domain`)."""
    known = list((catalog or get_catalog()).telemetry_domains())
    section = telemetry_section(cfg)
    defaults = section.get("defaults", {}) or {}
    domains_cfg = section.get("domains", {}) or {}
    unknown = set(domains_cfg) - set(known)
    if unknown:
        raise ValueError(f"ConfigValidationError: telemetry.domains has unknown domain(s) {sorted(unknown)}; "
                         f"the catalog defines {known}")
    parsed: dict[str, list[EndpointConfig]] = {}
    for domain in known:
        raw_eps = (domains_cfg.get(domain) or {}).get("endpoints")
        if raw_eps is None:
            raw_eps = [{"name": "local", "url": DEFAULT_INPROC_URL}]
        if not raw_eps:
            raise ValueError(f"ConfigValidationError: telemetry.domains.{domain}.endpoints must not be empty")
        eps = [_endpoint_config(raw, defaults) for raw in raw_eps]
        names = [ep.name for ep in eps]
        if len(set(names)) != len(names):
            raise ValueError(f"ConfigValidationError: telemetry.domains.{domain} has duplicate endpoint names {names}")
        parsed[domain] = eps
    return parsed


def _endpoint_coverage(client: MCPEndpointClient, response: ToolResponse, skipped: int) -> Coverage:
    """Collapse one endpoint's per-source coverage into a single entry."""
    non_ok = [c for c in response.coverage if c.status != "ok"]
    status = response.status
    errors = [f"{c.source}: {c.error or c.status}" for c in non_ok]
    if skipped:
        errors.append(f"{skipped} malformed signal(s) skipped")
        status = "partial" if status == "ok" else status
    lags = [c.lag_seconds for c in response.coverage if c.lag_seconds is not None]
    return Coverage(
        source=client.name,
        status=status,
        lag_seconds=max(lags) if lags else None,
        truncated=any(c.truncated for c in response.coverage) or response.next_cursor is not None,
        error="; ".join(errors) or None,
        # Failures confined to the server's optional sources don't make this
        # endpoint's data incomplete for degradation purposes.
        required=client.required and (not non_ok or any(c.required for c in non_ok) or bool(skipped)),
    )


TOPOLOGY_TTL_SECONDS = 60.0


class TelemetryRegistry(TelemetryProvider):
    def __init__(self, endpoints: dict[str, list[MCPEndpointClient]], tools: dict[str, str] | None = None,
                 topology_endpoints: list[MCPEndpointClient] | None = None) -> None:
        self._endpoints = endpoints
        self._tools = tools or {}
        if topology_endpoints is None:
            topology_endpoints = next(iter(endpoints.values()), [])
        self._topology_endpoints = topology_endpoints
        self._topology_cache: dict[tuple[str, int | None], tuple[float, TopologyResponse]] = {}
        self.recorder = None  # set by create_telemetry_provider when recording is on

    def endpoints(self, domain: str) -> list[MCPEndpointClient]:
        return list(self._endpoints.get(domain, []))

    async def fetch(self, domain: str, query: TelemetryQuery) -> ToolResponse:
        if domain not in self._endpoints:
            raise ValueError(f"unknown telemetry domain '{domain}'")
        clients = self._endpoints[domain]
        tool = self._tools.get(domain)
        results = await asyncio.gather(*(c.fetch(domain, query, tool) for c in clients), return_exceptions=True)

        coverage: list[Coverage] = []
        signals: list[Signal] = []
        seen: set[tuple[str, str]] = set()
        baseline_window = None
        for client, result in zip(clients, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.warning("Telemetry fetch failed endpoint=%s domain=%s error=%s", client.name, domain, result)
                coverage.append(Coverage(source=client.name, status="failed",
                                         error=f"{type(result).__name__}: {result}"[:500], required=client.required))
                continue
            response, skipped = result
            coverage.append(_endpoint_coverage(client, response, skipped))
            baseline_window = baseline_window or response.baseline_window
            for signal in response.signals:
                if signal.evidence_ref is not None:
                    key = (signal.source, signal.evidence_ref)
                    if key in seen:
                        continue
                    seen.add(key)
                signals.append(signal)
        response = ToolResponse(window=query.window, baseline_window=baseline_window, coverage=coverage, signals=signals)
        if self.recorder is not None:
            self.recorder.record(query, response)
        return response

    async def topology(self, region: str, scenario_id: int | None = None) -> TopologyResponse:
        """Entities and relationships from every topology endpoint, merged.
        Never raises: a failed endpoint becomes a failed coverage entry, and
        an empty graph just means collaboration has nothing to walk."""
        key = (region, scenario_id)
        cached = self._topology_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < TOPOLOGY_TTL_SECONDS:
            return cached[1]
        args: dict = {"region": region, "contract_version": CONTRACT_VERSION}
        if scenario_id is not None:
            args["scenario_id"] = scenario_id
        clients = self._topology_endpoints
        results = await asyncio.gather(*(c.call_tool("get_topology", args) for c in clients), return_exceptions=True)
        entities: dict[tuple[str, str], Entity] = {}
        edges: dict[tuple, TopologyEdge] = {}
        coverage: list[Coverage] = []
        for client, result in zip(clients, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.warning("Topology fetch failed endpoint=%s error=%s", client.name, result)
                coverage.append(Coverage(source=client.name, status="failed", error=f"{type(result).__name__}: {result}"[:500],
                                         required=False))
                continue
            try:
                response = TopologyResponse.model_validate(result)
            except Exception as exc:  # malformed payload: same treatment as a failed endpoint
                coverage.append(Coverage(source=client.name, status="failed", error=f"malformed topology: {exc}"[:500],
                                         required=False))
                continue
            statuses = {c.status for c in response.coverage} or {"ok"}
            status = "ok" if statuses == {"ok"} else ("failed" if statuses == {"failed"} else "partial")
            coverage.append(Coverage(source=client.name, status=status, required=False))
            for entity in response.entities:
                known = entities.get((entity.type, entity.id))
                entities[(entity.type, entity.id)] = (
                    entity if known is None else known.model_copy(update={"labels": {**entity.labels, **known.labels}})
                )
            for edge in response.relationships:
                edges[(edge.type, edge.source.type, edge.source.id, edge.target.type, edge.target.id)] = edge
        merged = TopologyResponse(region=region, entities=list(entities.values()), relationships=list(edges.values()),
                                  coverage=coverage)
        self._topology_cache[key] = (time.monotonic(), merged)
        if self.recorder is not None:
            self.recorder.record_topology(merged)
        return merged

    async def aclose(self) -> None:
        for clients in self._endpoints.values():
            for client in clients:
                await client.aclose()


def create_telemetry_provider(cfg: dict[str, Any], domains: tuple[str, ...] | None = None,
                              catalog: Catalog | None = None) -> TelemetryRegistry:
    """Build the registry for `domains` (default: all) from config.

    Endpoint clients are shared across domains when two domains list the
    same endpoint name + url, so they also share a session and a breaker.
    """
    catalog = catalog or get_catalog()
    parsed = parse_endpoints(cfg, catalog)
    tools = catalog.telemetry_domains()
    inproc_sources = telemetry_section(cfg).get("inproc_sources")
    shared: dict[tuple[str, str], MCPEndpointClient] = {}
    endpoints: dict[str, list[MCPEndpointClient]] = {}
    for domain in domains or tuple(tools):
        clients = []
        for ep in parsed[domain]:
            key = (ep.name, ep.url)
            if key not in shared:
                if ep.url.startswith(INPROC_SCHEME):
                    ensure_inproc_server(ep.url, inproc_sources)
                shared[key] = MCPEndpointClient(ep)
                logger.info("Telemetry endpoint domain=%s name=%s url=%s timeout=%ss required=%s",
                            domain, ep.name, ep.url, ep.timeout_seconds, ep.required)
            clients.append(shared[key])
        endpoints[domain] = clients
    topology_cfg = (telemetry_section(cfg).get("topology") or {}).get("endpoints")
    topology_clients = None
    if topology_cfg:
        defaults = telemetry_section(cfg).get("defaults", {}) or {}
        topology_clients = []
        for raw in topology_cfg:
            ep = _endpoint_config(raw, defaults)
            key = (ep.name, ep.url)
            if key not in shared:
                if ep.url.startswith(INPROC_SCHEME):
                    ensure_inproc_server(ep.url, inproc_sources)
                shared[key] = MCPEndpointClient(ep)
            topology_clients.append(shared[key])
    registry = TelemetryRegistry(endpoints, {d: tools[d] for d in endpoints}, topology_clients)
    record_dir = telemetry_section(cfg).get("record_dir") or os.environ.get(RECORD_DIR_ENV)
    if record_dir:
        from providers.recorder import TelemetryRecorder

        registry.recorder = TelemetryRecorder(record_dir)
        logger.info("Recording telemetry responses as replay fixtures under %s", record_dir)
    return registry
