"""Catalog overrides change detection end to end with zero code changes.

Each test points $NETCORTEX_CATALOG_DIR at an override directory of JSON
files (tests/fixtures/catalog_overrides/*) before the integration
fixtures build the agents and engine, then runs a real incident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contract.catalog_loader import CATALOG_ENV, reset_catalog_cache
from models.schemas import IncidentRequest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _latency_signal(cfg: dict) -> str:
    return str(FIXTURES / "catalog_overrides" / "latency_signal")


def _dns_domain(cfg: dict) -> str:
    # Telemetry config only: an in-process telemetry server with the replay
    # plugin pointed at the recorded NXDOMAIN fixtures, for every domain.
    url = "inproc://dns-override-e2e"
    cfg["telemetry"] = {
        "inproc_sources": {
            "simulation": {"enabled": True},
            "replay": {"enabled": True, "fixtures_dir": str(FIXTURES / "replay_dns")},
        },
        "domains": {d: {"endpoints": [{"name": "telemetry", "url": url}]}
                    for d in ("metrics", "logs", "routing", "config", "dns")},
    }
    return str(FIXTURES / "catalog_overrides" / "dns")


@pytest.fixture
def cfg(cfg, request, monkeypatch):
    configure = getattr(request, "param", None)
    reset_catalog_cache()
    if configure is not None:
        monkeypatch.setenv(CATALOG_ENV, configure(cfg))
    yield cfg
    monkeypatch.delenv(CATALOG_ENV, raising=False)
    reset_catalog_cache()


def _incident(scenario_id: int, description: str) -> IncidentRequest:
    return IncidentRequest(scenario_id=scenario_id, description=description, region="us-east", severity="high")


def _finding(report, domain):
    return next((f for f in report.agent_findings if f.domain == domain), None)


VOIP = "VoIP latency spike"  # scenario 3: EF-class latency 250ms, other metrics at baseline


@pytest.mark.asyncio
async def test_default_catalog_scenario_3_metrics_clean(engine):
    report = await engine.run_incident(_incident(3, VOIP))
    assert not _finding(report, "metrics").anomaly_detected


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_latency_signal], indirect=True)
async def test_override_only_new_signal_rule_changes_detection(engine):
    report = await engine.run_incident(_incident(3, VOIP))
    metrics = _finding(report, "metrics")
    assert metrics.anomaly_detected
    assert "latency above 200ms SLO" in metrics.summary
    assert metrics.key_events[0]["tags"] == {"dscp": "EF"}
    assert metrics.key_events[0]["rules_fired"][0]["rule"] == "latency_ms.threshold"


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_dns_domain], indirect=True)
async def test_override_only_new_dns_domain_appears_in_rca_report(engine):
    report = await engine.run_incident(_incident(1, "High error rate and throughput drop in us-east region"))

    dns = _finding(report, "dns")
    assert dns is not None, [f.domain for f in report.agent_findings]
    assert dns.agent_id == "dns"
    assert dns.anomaly_detected
    assert dns.summary.startswith("DNS failures: NXDOMAIN spike on 3 sample(s); resolver=rslvr-out-0abc1234; nxdomain_count=140")
    fired = dns.key_events[0]["rules_fired"]
    assert fired[0]["rule"] == "nxdomain_count.rate_change"
    assert fired[0]["evidence_ref"].startswith("logs:/aws/route53resolver/")
    # The signal outside the catalog is surfaced, not dropped and not fatal.
    assert [u["name"] for u in dns.unclassified_evidence] == ["ResolverEndpointHealth"]
    assert report.unclassified_evidence[0]["domain"] == "dns"
    assert "nxdomain_rate" in report.metrics_affected
    # Built-in domains still analyze simulation data as before.
    assert _finding(report, "metrics").anomaly_detected
    assert report.corroborating_domain_count == 3  # metrics, config, dns
