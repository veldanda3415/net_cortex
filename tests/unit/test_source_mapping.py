"""sources.json mappings: the simulation plugin must carry every scenario
field into contract v2 signals unchanged (replaces the PR A compat round-trip)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contract import TelemetryQuery
from contract.catalog_loader import get_catalog
from mcp_telemetry_server.mapping import map_entity
from mcp_telemetry_server.plugins.simulation import SimulationPlugin
from simulation.scenarios import SCENARIOS


def _query(scenario_id: int) -> TelemetryQuery:
    end = datetime.now(timezone.utc)
    return TelemetryQuery(region="us-east", start_time=end - timedelta(minutes=30), end_time=end, scenario_id=scenario_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
async def test_simulation_signals_carry_every_scenario_field(scenario_id):
    bundle = SCENARIOS[scenario_id]
    plugin = SimulationPlugin()
    q = _query(scenario_id)

    metrics, _ = await plugin.fetch(q, domain="metrics")
    for idx, row in enumerate(bundle.metrics_data):
        mine = {s.name: s for s in metrics if s.evidence_ref.startswith(f"simulation:scenario/{scenario_id}/metrics/{idx}/")}
        assert {n: s.value for n, s in mine.items()} == {
            "error_rate": row.error_rate, "packet_loss": row.packet_loss,
            "throughput_gbps": row.throughput_gbps, "latency_ms": row.latency_ms}
        assert all(s.entity.labels == row.tags and s.attributes["region"] == row.region for s in mine.values())

    logs, _ = await plugin.fetch(q, domain="logs")
    assert [(s.entity.id, s.attributes["level"], s.attributes["message"]) for s in logs] == [
        (r.service, r.level, r.message) for r in bundle.log_events]

    routing, _ = await plugin.fetch(q, domain="routing")
    assert [(s.entity.id, s.attributes["change_type"], s.attributes["details"]) for s in routing] == [
        (r.path_id, r.change_type, r.details) for r in bundle.routing_events]

    config, _ = await plugin.fetch(q, domain="config")
    assert [(s.entity.id, s.attributes["change_type"], s.attributes["before"], s.attributes["after"]) for s in config] == [
        (c.component, c.change_type, c.before, c.after) for c in bundle.config_changes]


def test_entity_mapping_priority_and_fallbacks():
    mapping = get_catalog().sources.mappings["simulation.metrics"]
    assert map_entity(mapping, {"tags": {"switch": "CORE-01", "interface": "Gi0/1"}, "region": "us-east"}).type == "interface"
    assert map_entity(mapping, {"tags": {"dscp": "EF"}, "region": "us-east"}).id == "EF"
    region_only = map_entity(mapping, {"tags": {}, "region": "us-east"})
    assert (region_only.type, region_only.id) == ("region", "us-east")
