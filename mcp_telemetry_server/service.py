"""TelemetryService — fans each tool call out to the enabled source plugins
for that domain and assembles one contract v2 response.

Stateless per request: every call builds its own coverage list, so
concurrent RCA tasks can never see each other's failures.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from contract import Coverage, Entity, TelemetryQuery, TimeWindow, ToolResponse, TopologyEdge, TopologyResponse
from contract.catalog_loader import get_catalog
from mcp_telemetry_server.plugins import SourcePlugin, get_plugin_class

logger = logging.getLogger("net_cortex.telemetry.service")

DEFAULT_SOURCES: dict[str, Any] = {"simulation": {"enabled": True}}
DEFAULT_PLUGIN_TIMEOUT_SECONDS = 10.0
BASELINE_LOOKBACK = timedelta(hours=24)
CONFIG_ENV = "NETCORTEX_TELEMETRY_CONFIG"


def load_sources_config(path: str | None = None) -> dict[str, Any]:
    """Read the `sources:` section from a telemetry config file.

    Resolution: explicit path, then $NETCORTEX_TELEMETRY_CONFIG, then the
    built-in default (simulation only) so a bare `python -m
    mcp_telemetry_server` keeps working with zero setup.
    """
    path = path or os.environ.get(CONFIG_ENV)
    if not path:
        return dict(DEFAULT_SOURCES)
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    sources = data.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ValueError(f"ConfigValidationError: {path} has no `sources:` section")
    return sources


class TelemetryService:
    def __init__(self, sources: dict[str, Any] | None = None, *, allow_cloud_sources: bool = True) -> None:
        sources = DEFAULT_SOURCES if sources is None else sources
        self.plugins: list[SourcePlugin] = []
        self._timeouts: dict[str, float] = {}
        for name, plugin_cfg in sources.items():
            plugin_cfg = plugin_cfg or {}
            if not plugin_cfg.get("enabled", False):
                continue
            cls = get_plugin_class(name)
            if not cls.implemented:
                raise ValueError(
                    f"ConfigValidationError: source plugin '{name}' is an interface skeleton and cannot be "
                    "enabled yet — see project_docs/provider-adapter-guide.md"
                )
            if cls.needs_cloud_credentials and not allow_cloud_sources:
                raise ValueError(
                    f"ConfigValidationError: source plugin '{name}' reads cloud APIs and may only run in a "
                    "telemetry pod, not in the RCA pod's in-process telemetry server"
                )
            plugin = cls(plugin_cfg)
            known = set(get_catalog().telemetry_domains()) | {"topology"}
            unknown = {d for d in plugin.domains if d != "*"} - known
            if unknown:
                raise ValueError(
                    f"ConfigValidationError: sources.{name}.domains has {sorted(unknown)}, but the catalog "
                    f"only serves {sorted(known)} (add the domain to contract/catalog/domains.json first)"
                )
            self.plugins.append(plugin)
            self._timeouts[name] = float(plugin_cfg.get("timeout_seconds", DEFAULT_PLUGIN_TIMEOUT_SECONDS))
        if not self.plugins:
            raise ValueError("ConfigValidationError: no telemetry source plugin is enabled")
        logger.info("Telemetry sources enabled: %s", ", ".join(p.name for p in self.plugins))

    def plugins_for(self, domain: str) -> list[SourcePlugin]:
        return [p for p in self.plugins if p.serves(domain)]

    async def query(self, domain: str, query: TelemetryQuery) -> ToolResponse:
        plugins = self.plugins_for(domain)
        baseline_window = (
            TimeWindow(start=query.start_time - BASELINE_LOOKBACK, end=query.start_time)
            if query.include_baseline
            else None
        )
        if not plugins:
            return ToolResponse(
                window=query.window,
                baseline_window=baseline_window,
                coverage=[Coverage(source="telemetry-server", status="failed",
                                   error=f"no enabled source serves domain '{domain}'")],
            )

        results = await asyncio.gather(
            *(asyncio.wait_for(p.fetch(query, domain=domain), timeout=self._timeouts[p.name]) for p in plugins),
            return_exceptions=True,
        )
        lower = baseline_window.start if baseline_window else query.start_time
        signals = []
        coverage: list[Coverage] = []
        for plugin, result in zip(plugins, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                error = "timeout" if isinstance(result, asyncio.TimeoutError) else f"{type(result).__name__}: {result}"
                logger.warning("Source fetch failed source=%s domain=%s error=%s", plugin.name, domain, error)
                coverage.append(plugin.coverage("failed", error=error))
                continue
            plugin_signals, plugin_coverage = result
            coverage.append(plugin_coverage)
            signals.extend(
                s for s in plugin_signals
                if lower <= s.timestamp <= query.end_time and query.matches(s)
            )
        return ToolResponse(window=query.window, baseline_window=baseline_window, coverage=coverage, signals=signals)

    async def topology(self, region: str, scenario_id: int | None) -> TopologyResponse:
        plugins = self.plugins_for("topology")
        if not plugins:
            return TopologyResponse(region=region, coverage=[
                Coverage(source="telemetry-server", status="failed", error="no enabled source serves topology")
            ])
        results = await asyncio.gather(
            *(asyncio.wait_for(p.topology(region, scenario_id), timeout=self._timeouts[p.name]) for p in plugins),
            return_exceptions=True,
        )
        entities: dict[tuple[str, str], Entity] = {}
        edges: dict[tuple[str, str, str, str, str], TopologyEdge] = {}
        coverage: list[Coverage] = []
        for plugin, result in zip(plugins, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                coverage.append(plugin.coverage("failed", error=f"{type(result).__name__}: {result}"))
                continue
            plugin_entities, plugin_edges, plugin_coverage = result
            coverage.append(plugin_coverage)
            for entity in plugin_entities:
                entities.setdefault((entity.type, entity.id), entity)
            for edge in plugin_edges:
                key = (edge.type, edge.source.type, edge.source.id, edge.target.type, edge.target.id)
                edges.setdefault(key, edge)
        return TopologyResponse(region=region, entities=list(entities.values()),
                                relationships=list(edges.values()), coverage=coverage)

    def health(self) -> dict[str, Any]:
        plugins: dict[str, Any] = {}
        for plugin in self.plugins:
            try:
                status = dict(plugin.health())
            except Exception as exc:  # health must never take the endpoint down
                status = {"status": "down", "error": f"{type(exc).__name__}: {exc}"}
            status["domains"] = sorted(plugin.domains)
            plugins[plugin.name] = status
        statuses = {p["status"] for p in plugins.values()}
        overall = "ok" if statuses == {"ok"} else ("down" if "ok" not in statuses else "degraded")
        return {"status": overall, "plugins": plugins}
