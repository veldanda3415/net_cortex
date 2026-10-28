"""Telemetry MCP server: source plugins, tools, version checks, health."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp import Client
from starlette.testclient import TestClient

from contract import TelemetryQuery, ToolResponse, TopologyResponse
from mcp_telemetry_server.plugins import get_plugin_class, registered_plugins
from mcp_telemetry_server.plugins.replay import ReplayPlugin
from mcp_telemetry_server.server import build_server
from mcp_telemetry_server.service import TelemetryService

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay"
RECORDED_END = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def _query(end: datetime, minutes: int = 30, region: str = "us-east-1", **kw) -> TelemetryQuery:
    return TelemetryQuery(region=region, start_time=end - timedelta(minutes=minutes), end_time=end, **kw)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_rebases_timestamps_into_query_window():
    plugin = ReplayPlugin({"fixtures_dir": str(FIXTURES)})
    end = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
    offset = end - RECORDED_END

    signals, coverage = await plugin.fetch(_query(end), domain="metrics")

    assert coverage.status == "ok"
    recorded = {"2026-09-14T09:40:00+00:00", "2026-09-14T09:52:00+00:00"}
    assert {(s.timestamp - offset).isoformat() for s in signals} == recorded
    # Spacing preserved exactly; event time and ingest lag both shifted.
    assert sorted(s.timestamp for s in signals) == [end - timedelta(minutes=20), end - timedelta(minutes=8)]
    assert all(s.observed_at - s.timestamp == timedelta(seconds=90) for s in signals)
    assert all(s.attributes["replay_fixture"] == "vpn_tunnel_down.json" for s in signals)
    assert all(s.source == "cloudwatch" for s in signals)  # original source preserved for dedupe


@pytest.mark.asyncio
async def test_replay_drops_rebased_signals_outside_window_and_other_regions():
    plugin = ReplayPlugin({"fixtures_dir": str(FIXTURES)})
    end = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)

    config_signals, _ = await plugin.fetch(_query(end), domain="config")
    assert config_signals == []  # recorded 40 min before the alarm, window is 30

    wider, _ = await plugin.fetch(_query(end, minutes=60), domain="config")
    assert [s.name for s in wider] == ["ModifyVpnTunnelOptions"]

    other_region, _ = await plugin.fetch(_query(end, region="eu-west-1"), domain="metrics")
    assert other_region == []


def test_replay_requires_fixtures_and_rejects_other_major_versions(tmp_path):
    with pytest.raises(ValueError, match="fixtures"):
        ReplayPlugin({})
    bad = tmp_path / "v1.json"
    data = json.loads((FIXTURES / "vpn_tunnel_down.json").read_text())
    bad.write_text(json.dumps({**data, "contract_version": "1.0"}))
    with pytest.raises(ValueError, match="major version"):
        ReplayPlugin({"fixtures": [str(bad)]})


# ---------------------------------------------------------------------------
# tools + contract version
# ---------------------------------------------------------------------------

def _window_args(minutes: int = 30) -> dict:
    end = datetime.now(timezone.utc)
    return {"start_time": (end - timedelta(minutes=minutes)).isoformat(), "end_time": end.isoformat()}


@pytest.mark.asyncio
async def test_tools_return_tool_response_with_coverage():
    server = build_server(TelemetryService())
    async with Client(server) as client:
        result = await client.call_tool("get_metrics", {"region": "us-east", "scenario_id": 1, **_window_args()})
    response = ToolResponse.model_validate(json.loads(result.content[0].text))
    assert response.contract_version == "2.1"
    assert [c.source for c in response.coverage] == ["simulation"]
    assert {s.name for s in response.signals} == {"error_rate", "packet_loss", "throughput_gbps", "latency_ms"}
    # Every signal carries a pointer back to its raw record, unique per signal.
    refs = [s.evidence_ref for s in response.signals]
    assert all(refs) and len(set(refs)) == len(refs)


@pytest.mark.asyncio
async def test_major_contract_version_mismatch_is_rejected_by_server():
    server = build_server(TelemetryService())
    async with Client(server) as client:
        result = await client.call_tool(
            "get_logs", {"region": "us-east", "contract_version": "1.0", **_window_args()}
        )
    assert result.is_error
    text = result.content[0].text
    assert "Unsupported contract_version '1.0'" in text and "major version 2" in text


@pytest.mark.asyncio
async def test_invalid_query_is_rejected_with_clear_error():
    server = build_server(TelemetryService())
    end = datetime.now(timezone.utc)
    async with Client(server) as client:
        result = await client.call_tool("get_metrics", {
            "region": "us-east", "start_time": end.isoformat(), "end_time": (end - timedelta(minutes=5)).isoformat(),
        })
    assert result.is_error and "start_time must be <= end_time" in result.content[0].text


@pytest.mark.asyncio
async def test_get_topology_returns_required_relationships():
    server = build_server(TelemetryService())
    async with Client(server) as client:
        result = await client.call_tool("get_topology", {"region": "us-east-1"})
    topo = TopologyResponse.model_validate(json.loads(result.content[0].text))
    chains = {(e.source.type, e.target.type) for e in topo.relationships}
    for pair in [("tunnel", "vpn_connection"), ("vpn_connection", "tgw_attachment"),
                 ("tgw_attachment", "tgw_route_table"), ("security_group", "eni"), ("eni", "instance"),
                 ("subnet", "route_table"), ("vpc", "vpc_peering"), ("vpc_peering", "vpc")]:
        assert pair in chains, pair
    assert topo.coverage[0].status == "ok"


@pytest.mark.asyncio
async def test_entity_filters_applied_by_service():
    service = TelemetryService()
    end = datetime.now(timezone.utc)
    query = _query(end, region="us-east", scenario_id=1, entity_filters=[{"type": "switch", "id": "C"}])
    response = await service.query("metrics", query)
    assert response.signals and {s.entity.id for s in response.signals} == {"C"}


@pytest.mark.asyncio
async def test_failing_plugin_becomes_failed_coverage_not_exception():
    service = TelemetryService({"simulation": {"enabled": True}, "replay": {"enabled": True, "fixtures_dir": str(FIXTURES)}})

    async def boom(query, *, domain):
        raise RuntimeError("log group not readable")

    service.plugins[1].fetch = boom  # type: ignore[method-assign]
    response = await service.query("metrics", _query(datetime.now(timezone.utc), region="us-east", scenario_id=1))
    assert response.status == "partial" and response.degraded
    failed = [c for c in response.coverage if c.status == "failed"]
    assert failed[0].source == "replay" and "log group not readable" in failed[0].error
    assert response.signals  # simulation data still returned


# ---------------------------------------------------------------------------
# plugins registry + skeletons
# ---------------------------------------------------------------------------

def test_all_required_plugins_registered():
    names = set(registered_plugins())
    assert {"simulation", "replay", "cloudwatch", "cloudwatch_logs", "cloudtrail", "tgw_routes", "frr_ssm",
            "prometheus", "elk", "splunk"} <= names


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["frr_ssm", "prometheus", "elk", "splunk"])
async def test_skeletons_raise_not_implemented_and_cannot_be_enabled(name):
    cls = get_plugin_class(name)
    domain = sorted(cls.domains)[0]
    with pytest.raises(NotImplementedError):
        await cls().fetch(_query(datetime.now(timezone.utc)), domain=domain)
    with pytest.raises(ValueError, match="skeleton"):
        TelemetryService({name: {"enabled": True}})


def test_unknown_plugin_and_empty_config_fail_fast():
    with pytest.raises(ValueError, match="unknown source plugin"):
        TelemetryService({"nope": {"enabled": True}})
    with pytest.raises(ValueError, match="no telemetry source"):
        TelemetryService({"simulation": {"enabled": False}})


def test_plugin_domains_can_be_narrowed_but_not_widened():
    service = TelemetryService({"replay": {"enabled": True, "fixtures_dir": str(FIXTURES), "domains": ["metrics"]}})
    assert service.plugins_for("logs") == []
    with pytest.raises(ValueError, match="only serves"):
        TelemetryService({"replay": {"enabled": True, "fixtures_dir": str(FIXTURES), "domains": ["bogus"]}})


# ---------------------------------------------------------------------------
# health / readiness
# ---------------------------------------------------------------------------

def test_health_and_ready_routes_report_per_plugin_status():
    service = TelemetryService({"simulation": {"enabled": True}, "replay": {"enabled": True, "fixtures_dir": str(FIXTURES)}})
    app = build_server(service).streamable_http_app()
    with TestClient(app) as http:
        health = http.get("/health")
        ready = http.get("/ready")
    assert health.status_code == 200 and ready.status_code == 200
    body = health.json()
    assert body["contract_version"] == "2.1" and body["status"] == "ok"
    assert set(body["plugins"]) == {"simulation", "replay"}
    assert body["plugins"]["replay"]["fixtures"] == ["vpn_tunnel_down.json"]


def test_ready_returns_503_when_no_plugin_is_usable():
    service = TelemetryService()
    service.plugins[0].health = lambda: {"status": "down", "error": "backend unreachable"}  # type: ignore[method-assign]
    with TestClient(build_server(service).streamable_http_app()) as http:
        assert http.get("/ready").status_code == 503
        assert http.get("/health").json()["plugins"]["simulation"]["status"] == "down"
