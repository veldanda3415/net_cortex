"""v2 -> legacy translation: simulation signals must round-trip to the exact
scenario rows the agents analyzed before, for every bundled scenario."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contract import Coverage, Entity, Signal, TelemetryQuery, TimeWindow, ToolResponse
from mcp_telemetry_server.plugins.simulation import SimulationPlugin
from providers.compat import to_config_changes, to_log_events, to_metric_snapshots, to_routing_events
from simulation.scenarios import SCENARIOS


def _strip(rows):
    return [r.model_dump(exclude={"timestamp"}) for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
async def test_simulation_round_trips_through_compat(scenario_id):
    bundle = SCENARIOS[scenario_id]
    end = datetime.now(timezone.utc)
    query = TelemetryQuery(region="us-east", start_time=end - timedelta(minutes=30), end_time=end, scenario_id=scenario_id)
    plugin = SimulationPlugin()

    async def fetch(domain):
        signals, coverage = await plugin.fetch(query, domain=domain)
        return ToolResponse(window=query.window, coverage=[coverage], signals=signals)

    assert _strip(to_metric_snapshots(await fetch("metrics"), "us-east")) == _strip(bundle.metrics_data)
    assert _strip(to_log_events(await fetch("logs"))) == _strip(bundle.log_events)
    assert _strip(to_routing_events(await fetch("routing"), "us-east")) == _strip(bundle.routing_events)
    assert _strip(to_config_changes(await fetch("config"))) == _strip(bundle.config_changes)


def test_signals_outside_legacy_shapes_are_skipped_not_fabricated():
    end = datetime.now(timezone.utc)
    window = TimeWindow(start=end - timedelta(minutes=30), end=end)

    def sig(**kw):
        base = dict(signal_type="metric", timestamp=end, observed_at=end, entity=Entity(type="tunnel", id="t1"),
                    name="error_rate", value=1.0, domain="metrics", source="cloudwatch")
        return Signal(**{**base, **kw})

    response = ToolResponse(window=window, coverage=[Coverage(source="x", status="ok")], signals=[
        sig(name="TunnelState", value=0.0),          # unknown to the legacy model
        sig(name="error_rate", value=3.0),           # incomplete snapshot (no throughput etc.)
        sig(signal_type="event", name="x", domain="routing", attributes={"change_type": "blackhole"}),
    ])
    assert to_metric_snapshots(response, "us-east-1") == []  # no throughput=0 invented
    assert to_routing_events(response, "us-east-1") == []
