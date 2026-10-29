"""Default-catalog detection against fixtures shaped like real AWS output
(CloudWatch GetMetricData, SearchTransitGatewayRoutes, CloudTrail
LookupEvents, FRR bgpd via SSM), replayed through the replay plugin."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contract import Coverage, Entity, Signal, TelemetryQuery, TimeWindow, ToolResponse
from contract.catalog_loader import load_catalog
from detection import analyze_domain
from mcp_telemetry_server.plugins.replay import ReplayPlugin

AWS = Path(__file__).resolve().parents[1] / "fixtures" / "aws"
CATALOG = load_catalog()
END = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
QUERY = TelemetryQuery(region="us-east-1", start_time=END - timedelta(minutes=30), end_time=END)


async def _findings(fixture: str, description: str = "Site-to-site connectivity loss to on-prem 10.100.0.0/16"):
    plugin = ReplayPlugin({"fixtures": [str(AWS / fixture)]})
    out = {}
    for domain_id in CATALOG.domain_ids:
        domain = CATALOG.domain(domain_id)
        signals, coverage = await plugin.fetch(QUERY, domain=domain.telemetry_domain)
        response = ToolResponse(window=QUERY.window, coverage=[coverage], signals=signals)
        out[domain_id] = analyze_domain(CATALOG, domain_id, response, region="us-east-1",
                                        incident_description=description, baseline_lookup=lambda k, m: None)
    return out


def _fired(finding) -> list[dict]:
    return [r for e in finding.key_events for r in e.get("rules_fired", [])]


@pytest.mark.asyncio
async def test_background_noise_only_produces_no_anomaly():
    findings = await _findings("background_noise.json")
    for domain_id, finding in findings.items():
        assert not finding.anomaly_detected, (domain_id, finding.summary)
        assert not finding.data_degraded
        assert finding.unclassified_evidence == [], domain_id   # Describe* etc. are catalogued noise, not unknown
    assert findings["config"].summary == "No incident-relevant config changes"
    assert findings["metrics"].summary == "Metrics within baseline"


@pytest.mark.asyncio
async def test_one_of_two_tunnels_down_with_tgw_blackhole_and_bgp_error():
    findings = await _findings("one_tunnel_down.json")

    metrics = findings["metrics"]
    assert metrics.anomaly_detected
    fired = _fired(metrics)
    assert {f["rule"] for f in fired} == {"TunnelState.state_equals", "TunnelDataIn.rate_change"}
    assert {e["entity_id"] for e in metrics.key_events} == {"vpn-0a1b2c3d-tunnel-1"}   # tunnel-2 stayed up
    assert all(f["evidence_ref"].startswith("cloudwatch:AWS/VPN/") for f in fired)
    assert "worst tunnel=vpn-0a1b2c3d-tunnel-1" in metrics.summary

    routing = findings["routing"]
    assert routing.anomaly_detected
    blackhole = [e for e in routing.key_events if e.get("rules_fired")]
    assert len(blackhole) == 1 and blackhole[0]["state"] == "blackhole"
    assert blackhole[0]["rules_fired"][0]["rule"] == "tgw_route.state_equals"
    assert blackhole[0]["raw"]["State"] == "blackhole"                                # raw AWS payload kept
    assert "Routing anomalies found: 1 event(s); first=tgw_route_table:tgw-rtb-0core0001 (TGW route blackhole)" == routing.summary

    log = findings["log"]
    assert log.anomaly_detected and "top_service=169.254.10.1" in log.summary
    assert [f["rule"] for f in _fired(log)] == ["frr_log.event_presence"]

    assert not findings["config"].anomaly_detected                                   # only Describe* reads


@pytest.mark.asyncio
async def test_cloudtrail_revoke_security_group_ingress_is_detected_and_traceable():
    config = (await _findings("sg_revoke.json"))["config"]
    assert config.anomaly_detected
    assert len(config.key_events) == 1                                                # the 5 reads are not evidence
    event = config.key_events[0]
    assert event["name"] == "RevokeSecurityGroupIngress"
    assert event["entity_id"] == "sg-0app00001"
    assert event["rules_fired"] == [{
        "signal": "RevokeSecurityGroupIngress", "rule": "cloudtrail_network_mutation.event_presence",
        "reason": "RevokeSecurityGroupIngress (info)", "evidence_ref": "2f0c9a1e-6b3d-4e5f-9a7b-c1d2e3f40506",
    }]
    assert "latest=RevokeSecurityGroupIngress on security_group:sg-0app00001" in config.summary


def test_degraded_domain_is_never_reported_clean():
    window = TimeWindow(start=QUERY.start_time, end=QUERY.end_time)
    failed = ToolResponse(window=window, coverage=[Coverage(source="aws-telemetry", status="failed", error="timeout")])
    finding = analyze_domain(CATALOG, "routing", failed, region="us-east-1")
    assert finding.data_degraded and not finding.anomaly_detected
    assert "NOT verified clean" in finding.summary and "aws-telemetry: timeout" in finding.summary
    assert finding.summary != "No routing changes"


def test_unknown_signals_are_kept_as_unclassified_evidence_not_errors():
    ts = QUERY.end_time - timedelta(minutes=2)
    unknown = Signal(signal_type="metric", timestamp=ts, observed_at=ts, name="NatGatewayErrorPortAllocation", value=37,
                     entity=Entity(type="nat_gateway", id="nat-0abc"), domain="metrics", source="cloudwatch",
                     evidence_ref="cloudwatch:AWS/NATGateway/ErrorPortAllocation@x")
    response = ToolResponse(window=QUERY.window, coverage=[Coverage(source="x", status="ok")], signals=[unknown])
    finding = analyze_domain(CATALOG, "metrics", response, region="us-east-1")
    assert not finding.anomaly_detected                       # no rule judged it
    assert finding.unclassified_evidence == [{
        "name": "NatGatewayErrorPortAllocation", "signal_type": "metric", "source": "cloudwatch",
        "entity": "nat_gateway:nat-0abc", "value": 37.0, "severity": "info", "timestamp": ts.isoformat(),
        "evidence_ref": "cloudwatch:AWS/NATGateway/ErrorPortAllocation@x", "count": 1,
        "evidence_refs": ["cloudwatch:AWS/NATGateway/ErrorPortAllocation@x"], "last_seen": ts.isoformat(),
        "min": 37.0, "max": 37.0,
    }]
