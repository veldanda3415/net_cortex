"""Unit tests for providers/adapters/mcp_adapter.py.

Covers the adapter-guide contract directly: deterministic return shapes,
never-raises failure handling, malformed-row skipping, and the sync/async
bridge that lets these synchronous provider methods be called from both a
plain sync context and from inside an already-running event loop (the
agents call these methods from async FastAPI handlers).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from providers.adapters.mcp_adapter import (
    MCPConfigAdapter,
    MCPLogAdapter,
    MCPMetricsAdapter,
    MCPRoutingAdapter,
)


def _tool_result(payload: dict) -> SimpleNamespace:
    """Shape a fake MCP tool_call result matching result.content[0].text."""
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])


class _FakeMCPClient:
    """Fake async context manager standing in for mcp.Client."""

    def __init__(self, endpoint, *, response=None, raise_on_call: Exception | None = None):
        self._response = response
        self._raise_on_call = raise_on_call
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        if self._raise_on_call:
            raise self._raise_on_call
        return self._response


def _patch_client(response=None, raise_on_call=None):
    fake = _FakeMCPClient(None, response=response, raise_on_call=raise_on_call)
    return patch("providers.adapters.mcp_adapter.Client", return_value=fake), fake


# ---------------------------------------------------------------------------
# MCPMetricsAdapter
# ---------------------------------------------------------------------------

def test_metrics_adapter_parses_valid_rows():
    response = _tool_result({"metrics": [
        {
            "timestamp": "2026-05-05T10:14:00Z", "region": "us-east",
            "error_rate": 0.081, "packet_loss": 0.024, "throughput_gbps": 0.61,
            "latency_ms": 124.5, "tags": {"service": "gateway"},
        },
    ]})
    patcher, fake = _patch_client(response=response)
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_metrics("us-east", 30, scenario_id=1)

    assert len(rows) == 1
    assert rows[0].region == "us-east"
    assert rows[0].error_rate == 0.081
    assert rows[0].tags == {"service": "gateway"}
    assert fake.calls == [("get_metrics", {"region": "us-east", "window_minutes": 30, "scenario_id": 1})]


def test_metrics_adapter_skips_malformed_rows_without_raising():
    response = _tool_result({"metrics": [
        {"timestamp": "2026-05-05T10:14:00Z", "region": "us-east", "error_rate": "not-a-number",
         "packet_loss": 0.0, "throughput_gbps": 1.0, "latency_ms": 50},
        {"timestamp": "2026-05-05T10:15:00Z", "region": "us-east", "error_rate": 0.5,
         "packet_loss": 0.2, "throughput_gbps": 1.0, "latency_ms": 50},
    ]})
    patcher, _ = _patch_client(response=response)
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_metrics("us-east", 30, scenario_id=1)

    assert len(rows) == 1  # first row skipped, second parsed


def test_metrics_adapter_missing_key_returns_empty_list():
    response = _tool_result({"metrics": [{"region": "us-east"}]})  # missing required fields
    patcher, _ = _patch_client(response=response)
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_metrics("us-east", 30, scenario_id=1)
    assert rows == []


def test_metrics_adapter_empty_metrics_key_returns_empty_list():
    response = _tool_result({})  # no "metrics" key at all
    patcher, _ = _patch_client(response=response)
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_metrics("us-east", 30, scenario_id=1)
    assert rows == []


def test_metrics_adapter_connection_failure_never_raises():
    patcher, _ = _patch_client(raise_on_call=ConnectionError("telemetry unreachable"))
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_metrics("us-east", 30, scenario_id=1)  # must not raise
    assert rows == []


def test_metrics_adapter_defaults_scenario_id_to_1_when_none():
    response = _tool_result({"metrics": []})
    patcher, fake = _patch_client(response=response)
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        adapter.get_metrics("us-east", 30, scenario_id=None)
    assert fake.calls[0][1]["scenario_id"] == 1


# ---------------------------------------------------------------------------
# MCPLogAdapter / MCPRoutingAdapter / MCPConfigAdapter — one solid pass each,
# mirroring the metrics coverage above without repeating every case.
# ---------------------------------------------------------------------------

def test_log_adapter_parses_valid_rows():
    response = _tool_result({"logs": [
        {"timestamp": "2026-05-05T10:14:00Z", "level": "ERROR", "service": "api-gw", "message": "boom"},
    ]})
    patcher, fake = _patch_client(response=response)
    with patcher:
        adapter = MCPLogAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_logs("us-east", 30, scenario_id=2)
    assert len(rows) == 1
    assert rows[0].level == "ERROR"
    assert fake.calls == [("get_logs", {"region": "us-east", "window_minutes": 30, "scenario_id": 2})]


def test_log_adapter_never_raises_on_failure():
    patcher, _ = _patch_client(raise_on_call=TimeoutError("slow telemetry"))
    with patcher:
        adapter = MCPLogAdapter(endpoint="http://telemetry:9001/mcp")
        assert adapter.get_logs("us-east", 30, scenario_id=1) == []


def test_routing_adapter_parses_valid_rows():
    response = _tool_result({"routing_events": [
        {"timestamp": "2026-05-05T10:14:00Z", "region": "us-east", "path_id": "us-east-core",
         "change_type": "reroute", "details": "A->B->D to A->C->D"},
    ]})
    patcher, _ = _patch_client(response=response)
    with patcher:
        adapter = MCPRoutingAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_routing_events("us-east", 30, scenario_id=9)
    assert len(rows) == 1
    assert rows[0].change_type == "reroute"


def test_routing_adapter_never_raises_on_failure():
    patcher, _ = _patch_client(raise_on_call=OSError("dns failure"))
    with patcher:
        adapter = MCPRoutingAdapter(endpoint="http://telemetry:9001/mcp")
        assert adapter.get_routing_events("us-east", 30, scenario_id=1) == []


def test_config_adapter_parses_valid_rows():
    response = _tool_result({"config_changes": [
        {"timestamp": "2026-05-05T10:14:00Z", "component": "Switch-C eth0/1", "change_type": "bandwidth_limit",
         "before": {"capacity_gbps": 10}, "after": {"capacity_gbps": 5}},
    ]})
    patcher, _ = _patch_client(response=response)
    with patcher:
        adapter = MCPConfigAdapter(endpoint="http://telemetry:9001/mcp")
        rows = adapter.get_config_changes("us-east", 30, scenario_id=1)
    assert len(rows) == 1
    assert rows[0].after == {"capacity_gbps": 5}


def test_config_adapter_never_raises_on_failure():
    patcher, _ = _patch_client(raise_on_call=RuntimeError("mcp server 500"))
    with patcher:
        adapter = MCPConfigAdapter(endpoint="http://telemetry:9001/mcp")
        assert adapter.get_config_changes("us-east", 30, scenario_id=1) == []


# ---------------------------------------------------------------------------
# Sync/async bridge: same adapter call must work whether or not an event
# loop is already running in the calling context.
# ---------------------------------------------------------------------------

def test_get_metrics_works_from_plain_sync_context():
    response = _tool_result({"metrics": []})
    patcher, _ = _patch_client(response=response)
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        # No event loop running here — exercises the asyncio.run() branch.
        assert adapter.get_metrics("us-east", 30, scenario_id=1) == []


@pytest.mark.asyncio
async def test_get_metrics_works_from_inside_running_event_loop():
    response = _tool_result({"metrics": [
        {"timestamp": "2026-05-05T10:14:00Z", "region": "us-east", "error_rate": 0.5,
         "packet_loss": 0.2, "throughput_gbps": 1.0, "latency_ms": 50},
    ]})
    patcher, _ = _patch_client(response=response)
    with patcher:
        adapter = MCPMetricsAdapter(endpoint="http://telemetry:9001/mcp")
        # This test coroutine runs on pytest-asyncio's event loop. Calling the
        # *sync* get_metrics() directly here — not via asyncio.to_thread,
        # which would move to a fresh thread with no loop at all — means
        # asyncio.get_running_loop() inside _run_async sees a loop IS
        # running on this thread, forcing the ThreadPoolExecutor bridge
        # path. That's the exact situation the agents hit when calling
        # providers from inside async FastAPI handlers.
        rows = adapter.get_metrics("us-east", 30, 1)
    assert len(rows) == 1
