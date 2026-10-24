"""Unit tests for providers/adapters/mcp_adapter.py + the registry around it.

Covers the adapter-guide contract on the v2 client: deterministic return
shapes, failures surfacing as coverage (never as an exception out of
registry.fetch), malformed-signal skipping, the exact tool arguments sent,
and session reuse (one MCP session per endpoint, not one per call).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from contract import TelemetryQuery
from providers.adapters.mcp_adapter import EndpointConfig, MCPEndpointClient
from providers.compat import to_config_changes, to_log_events, to_metric_snapshots, to_routing_events
from providers.factory import TelemetryRegistry
from providers.resilience import RetryPolicy

END = datetime(2026, 5, 5, 10, 30, tzinfo=timezone.utc)
START = END - timedelta(minutes=30)
TS = "2026-05-05T10:14:00Z"


def _tool_result(payload: dict) -> SimpleNamespace:
    """Shape a fake MCP tool_call result matching result.content[0].text."""
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))], is_error=False, structured_content=None)


def _response(signals: list[dict]) -> dict:
    return {
        "contract_version": "2.0",
        "window": {"start": START.isoformat(), "end": END.isoformat()},
        "coverage": [{"source": "simulation", "status": "ok"}],
        "signals": signals,
    }


def _signal(**overrides) -> dict:
    base = {
        "signal_type": "metric", "timestamp": TS, "observed_at": TS,
        "entity": {"type": "service", "id": "gateway", "labels": {"service": "gateway"}},
        "name": "error_rate", "value": 0.5, "domain": "metrics", "source": "simulation",
        "attributes": {"region": "us-east"}, "evidence_ref": "ref-1",
    }
    base.update(overrides)
    return base


def _metric_row(values: dict[str, float], ref: str = "row") -> list[dict]:
    return [_signal(name=n, value=v, evidence_ref=f"{ref}/{n}") for n, v in values.items()]


class _FakeMCPClient:
    def __init__(self, *, response=None, raise_on_call: Exception | None = None):
        self._response = response
        self._raise_on_call = raise_on_call
        self.calls: list[tuple[str, dict]] = []
        self.sessions_opened = 0

    def factory(self):
        @asynccontextmanager
        async def _cm():
            self.sessions_opened += 1
            yield self
        return _cm()

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        await asyncio.sleep(0)
        if self._raise_on_call:
            raise self._raise_on_call
        return self._response


def _registry(domain: str, fake: _FakeMCPClient) -> TelemetryRegistry:
    config = EndpointConfig(name="telemetry", url="http://telemetry:9001/mcp", retry=RetryPolicy(max_attempts=1))
    return TelemetryRegistry({domain: [MCPEndpointClient(config, client_factory=fake.factory)]})


def _query(scenario_id: int | None = 1) -> TelemetryQuery:
    return TelemetryQuery(region="us-east", start_time=START, end_time=END, scenario_id=scenario_id)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_adapter_parses_valid_rows():
    fake = _FakeMCPClient(response=_tool_result(_response(_metric_row(
        {"error_rate": 0.081, "packet_loss": 0.024, "throughput_gbps": 0.61, "latency_ms": 124.5}))))
    response = await _registry("metrics", fake).fetch("metrics", _query())
    rows = to_metric_snapshots(response, "us-east")

    assert len(rows) == 1
    assert rows[0].region == "us-east"
    assert rows[0].error_rate == 0.081
    assert rows[0].tags == {"service": "gateway"}
    assert not response.degraded
    assert fake.calls == [("get_metrics", {
        "region": "us-east", "start_time": START.isoformat().replace("+00:00", "Z"),
        "end_time": END.isoformat().replace("+00:00", "Z"), "include_baseline": False,
        "scenario_id": 1, "contract_version": "2.0",
    })]


@pytest.mark.asyncio
async def test_metrics_adapter_skips_malformed_rows_without_raising():
    signals = [_signal(value="not-a-number", evidence_ref="bad")] + _metric_row(
        {"error_rate": 0.5, "packet_loss": 0.2, "throughput_gbps": 1.0, "latency_ms": 50})
    fake = _FakeMCPClient(response=_tool_result(_response(signals)))
    response = await _registry("metrics", fake).fetch("metrics", _query())

    assert len(response.signals) == 4  # malformed one skipped, the rest parsed
    assert len(to_metric_snapshots(response, "us-east")) == 1
    assert response.coverage[0].status == "partial"
    assert "1 malformed signal" in response.coverage[0].error


@pytest.mark.asyncio
async def test_metrics_adapter_missing_key_returns_empty_list():
    fake = _FakeMCPClient(response=_tool_result(_response([{"signal_type": "metric"}])))  # missing required fields
    response = await _registry("metrics", fake).fetch("metrics", _query())
    assert response.signals == []
    assert response.degraded


@pytest.mark.asyncio
async def test_metrics_adapter_empty_signals_is_verified_no_data():
    fake = _FakeMCPClient(response=_tool_result(_response([])))
    response = await _registry("metrics", fake).fetch("metrics", _query())
    assert response.signals == []
    assert response.status == "ok" and not response.degraded  # "no data", not "data unavailable"


@pytest.mark.asyncio
async def test_metrics_adapter_connection_failure_never_raises():
    fake = _FakeMCPClient(raise_on_call=ConnectionError("telemetry unreachable"))
    response = await _registry("metrics", fake).fetch("metrics", _query())  # must not raise
    assert response.signals == []
    assert response.coverage[0].status == "failed"
    assert response.degraded


@pytest.mark.asyncio
async def test_metrics_adapter_omits_scenario_id_when_none():
    fake = _FakeMCPClient(response=_tool_result(_response([])))
    await _registry("metrics", fake).fetch("metrics", _query(scenario_id=None))
    assert "scenario_id" not in fake.calls[0][1]


# ---------------------------------------------------------------------------
# logs / routing / config — one solid pass each.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_adapter_parses_valid_rows():
    fake = _FakeMCPClient(response=_tool_result(_response([_signal(
        signal_type="event", name="log_message", value=None, domain="logs", severity="error",
        entity={"type": "service", "id": "api-gw"}, attributes={"level": "ERROR", "message": "boom"})])))
    response = await _registry("logs", fake).fetch("logs", _query(scenario_id=2))
    rows = to_log_events(response)
    assert len(rows) == 1
    assert rows[0].level == "ERROR" and rows[0].service == "api-gw"
    assert fake.calls[0][0] == "get_logs" and fake.calls[0][1]["scenario_id"] == 2


@pytest.mark.asyncio
async def test_log_adapter_never_raises_on_failure():
    fake = _FakeMCPClient(raise_on_call=TimeoutError("slow telemetry"))
    response = await _registry("logs", fake).fetch("logs", _query())
    assert to_log_events(response) == [] and response.degraded


@pytest.mark.asyncio
async def test_routing_adapter_parses_valid_rows():
    fake = _FakeMCPClient(response=_tool_result(_response([_signal(
        signal_type="event", name="routing_change", value=None, domain="routing",
        entity={"type": "path", "id": "us-east-core"},
        attributes={"change_type": "reroute", "details": "A->B->D to A->C->D", "region": "us-east"})])))
    response = await _registry("routing", fake).fetch("routing", _query(scenario_id=9))
    rows = to_routing_events(response, "us-east")
    assert len(rows) == 1
    assert rows[0].change_type == "reroute"


@pytest.mark.asyncio
async def test_routing_adapter_never_raises_on_failure():
    fake = _FakeMCPClient(raise_on_call=OSError("dns failure"))
    response = await _registry("routing", fake).fetch("routing", _query())
    assert to_routing_events(response, "us-east") == [] and response.degraded


@pytest.mark.asyncio
async def test_config_adapter_parses_valid_rows():
    fake = _FakeMCPClient(response=_tool_result(_response([_signal(
        signal_type="event", name="config_change", value=None, domain="config",
        entity={"type": "component", "id": "Switch-C eth0/1"},
        attributes={"change_type": "bandwidth_limit", "before": {"capacity_gbps": 10}, "after": {"capacity_gbps": 5}})])))
    response = await _registry("config", fake).fetch("config", _query())
    rows = to_config_changes(response)
    assert len(rows) == 1
    assert rows[0].after == {"capacity_gbps": 5}


@pytest.mark.asyncio
async def test_config_adapter_never_raises_on_failure():
    fake = _FakeMCPClient(raise_on_call=RuntimeError("mcp server 500"))
    response = await _registry("config", fake).fetch("config", _query())
    assert to_config_changes(response) == [] and response.degraded


# ---------------------------------------------------------------------------
# Session reuse: replaces the old thread + event-loop-per-call bridge.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_is_reused_across_sequential_calls():
    fake = _FakeMCPClient(response=_tool_result(_response([])))
    registry = _registry("metrics", fake)
    for _ in range(3):
        await registry.fetch("metrics", _query())
    assert len(fake.calls) == 3
    assert fake.sessions_opened == 1


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_session():
    fake = _FakeMCPClient(response=_tool_result(_response([])))
    registry = _registry("metrics", fake)
    await asyncio.gather(*(registry.fetch("metrics", _query()) for _ in range(5)))
    assert len(fake.calls) == 5
    assert fake.sessions_opened == 1
