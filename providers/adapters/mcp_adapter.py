"""MCP Provider Adapters — fetch telemetry from an external MCP Telemetry Server.

These adapters implement the provider interfaces defined in providers/base.py
and connect to an MCP server (2026-07-28 spec) to fetch metrics, logs, routing
events, and config changes.

Each adapter:
  1. Connects to an external MCP server endpoint (from config.yaml)
  2. Calls the appropriate MCP tool
  3. Maps the response to canonical Pydantic models
  4. Handles timeouts/failures gracefully (returns empty list, never crashes)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from mcp import Client

from models.schemas import ConfigChange, LogEvent, MetricSnapshot, RoutingEvent
from providers.base import ConfigProvider, LogProvider, MetricsProvider, RoutingProvider

logger = logging.getLogger("net_cortex.adapter.mcp")


def _run_async(coro) -> Any:
    """Run an async coroutine from sync context safely.

    Handles the case where an event loop is already running (e.g., inside
    the orchestrator's async context) by creating a new thread with its own loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop running — safe to use asyncio.run()
        return asyncio.run(coro)

    # Loop already running — use a thread to avoid nested event loop issues
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=60)


class MCPMetricsAdapter(MetricsProvider):
    """Fetches metrics telemetry from an external MCP server."""

    def __init__(self, endpoint: str, timeout: int = 30) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None) -> list[MetricSnapshot]:
        """Fetch metrics from MCP Telemetry Server."""
        try:
            return _run_async(self._fetch(region, window_minutes, scenario_id))
        except Exception as e:
            logger.warning("MCP metrics fetch failed endpoint=%s error=%s", self._endpoint, e)
            return []

    async def _fetch(self, region: str, window_minutes: int, scenario_id: int | None) -> list[MetricSnapshot]:
        async with Client(self._endpoint) as client:
            result = await client.call_tool("get_metrics", {
                "region": region,
                "window_minutes": window_minutes,
                "scenario_id": scenario_id or 1,
            })
            return self._parse(result)

    def _parse(self, result) -> list[MetricSnapshot]:
        data = json.loads(result.content[0].text)
        snapshots = []
        for row in data.get("metrics", []):
            try:
                snapshots.append(MetricSnapshot(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    region=row["region"],
                    error_rate=float(row["error_rate"]),
                    packet_loss=float(row["packet_loss"]),
                    throughput_gbps=float(row["throughput_gbps"]),
                    latency_ms=float(row["latency_ms"]),
                    tags=row.get("tags", {}),
                ))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Skipping malformed metric row: %s", e)
        return snapshots


class MCPLogAdapter(LogProvider):
    """Fetches log events from an external MCP server."""

    def __init__(self, endpoint: str, timeout: int = 30) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None) -> list[LogEvent]:
        """Fetch logs from MCP Telemetry Server."""
        try:
            return _run_async(self._fetch(region, window_minutes, scenario_id))
        except Exception as e:
            logger.warning("MCP logs fetch failed endpoint=%s error=%s", self._endpoint, e)
            return []

    async def _fetch(self, region: str, window_minutes: int, scenario_id: int | None) -> list[LogEvent]:
        async with Client(self._endpoint) as client:
            result = await client.call_tool("get_logs", {
                "region": region,
                "window_minutes": window_minutes,
                "scenario_id": scenario_id or 1,
            })
            return self._parse(result)

    def _parse(self, result) -> list[LogEvent]:
        data = json.loads(result.content[0].text)
        events = []
        for row in data.get("logs", []):
            try:
                events.append(LogEvent(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    level=row["level"],
                    service=row["service"],
                    message=row["message"],
                ))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Skipping malformed log row: %s", e)
        return events


class MCPRoutingAdapter(RoutingProvider):
    """Fetches routing events from an external MCP server."""

    def __init__(self, endpoint: str, timeout: int = 30) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    def get_routing_events(self, region: str, window_minutes: int, scenario_id: int | None) -> list[RoutingEvent]:
        """Fetch routing events from MCP Telemetry Server."""
        try:
            return _run_async(self._fetch(region, window_minutes, scenario_id))
        except Exception as e:
            logger.warning("MCP routing fetch failed endpoint=%s error=%s", self._endpoint, e)
            return []

    async def _fetch(self, region: str, window_minutes: int, scenario_id: int | None) -> list[RoutingEvent]:
        async with Client(self._endpoint) as client:
            result = await client.call_tool("get_routing_events", {
                "region": region,
                "window_minutes": window_minutes,
                "scenario_id": scenario_id or 1,
            })
            return self._parse(result)

    def _parse(self, result) -> list[RoutingEvent]:
        data = json.loads(result.content[0].text)
        events = []
        for row in data.get("routing_events", []):
            try:
                events.append(RoutingEvent(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    region=row["region"],
                    path_id=row["path_id"],
                    change_type=row["change_type"],
                    details=row["details"],
                ))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Skipping malformed routing row: %s", e)
        return events


class MCPConfigAdapter(ConfigProvider):
    """Fetches configuration changes from an external MCP server."""

    def __init__(self, endpoint: str, timeout: int = 30) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    def get_config_changes(self, region: str, window_minutes: int, scenario_id: int | None) -> list[ConfigChange]:
        """Fetch config changes from MCP Telemetry Server."""
        try:
            return _run_async(self._fetch(region, window_minutes, scenario_id))
        except Exception as e:
            logger.warning("MCP config fetch failed endpoint=%s error=%s", self._endpoint, e)
            return []

    async def _fetch(self, region: str, window_minutes: int, scenario_id: int | None) -> list[ConfigChange]:
        async with Client(self._endpoint) as client:
            result = await client.call_tool("get_config_changes", {
                "region": region,
                "window_minutes": window_minutes,
                "scenario_id": scenario_id or 1,
            })
            return self._parse(result)

    def _parse(self, result) -> list[ConfigChange]:
        data = json.loads(result.content[0].text)
        changes = []
        for row in data.get("config_changes", []):
            try:
                changes.append(ConfigChange(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    component=row["component"],
                    change_type=row["change_type"],
                    before=row["before"],
                    after=row["after"],
                ))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Skipping malformed config row: %s", e)
        return changes
