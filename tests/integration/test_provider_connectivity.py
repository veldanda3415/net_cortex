"""Provider connectivity through the real agents + engine.

Uses the integration conftest's in-process agents/engine, with the
telemetry section of the config overridden per test (indirect `cfg`
parametrization) so every fetch goes registry -> MCP -> telemetry server ->
source plugins exactly as in production.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from contract import Coverage, Signal, TelemetryQuery
from ingestion.incident_normalizer import normalize_incident
from mcp_telemetry_server.plugins import SourcePlugin, register_plugin
from mcp_telemetry_server.server import build_server
from mcp_telemetry_server.service import TelemetryService
from models.schemas import IncidentRequest
from providers.inproc import register_inproc_server
from providers.query import query_from_request

DOWN_URL = "http://127.0.0.1:1/mcp"
RECORDED_QUERIES: list[tuple[str, TelemetryQuery]] = []


@register_plugin
class _ScenarioOutagePlugin(SourcePlugin):
    """Metrics backend that is down only for scenario 2, and slow either way,
    so two concurrent incidents' fetches overlap in time."""

    name = "test_scenario_outage"
    domains = frozenset({"metrics"})

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        if query.scenario_id == 2:
            await asyncio.sleep(0.05)
            raise RuntimeError("backend outage for scenario 2")
        await asyncio.sleep(0.15)
        return [], self.coverage()


@register_plugin
class _RecordingPlugin(SourcePlugin):
    name = "test_recorder"
    domains = frozenset({"metrics", "logs", "routing", "config"})

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        RECORDED_QUERIES.append((domain, query))
        return [], self.coverage()


def _all_domains(endpoints: list[dict]) -> dict[str, Any]:
    return {d: {"endpoints": endpoints} for d in ("metrics", "logs", "routing", "config")}


def _with_endpoint_down(cfg: dict) -> None:
    cfg["telemetry"] = {"domains": {
        **_all_domains([{"name": "local", "url": "inproc://local"}]),
        "metrics": {"endpoints": [
            {"name": "primary", "url": "inproc://local"},
            {"name": "secondary", "url": DOWN_URL, "timeout_seconds": 2, "retry": {"max_attempts": 1}},
        ]},
    }}


def _with_scenario_outage(cfg: dict) -> None:
    url = "inproc://scenario-outage"
    register_inproc_server(url, build_server(TelemetryService(
        {"simulation": {"enabled": True}, "test_scenario_outage": {"enabled": True}})))
    cfg["telemetry"] = {"domains": _all_domains([{"name": "telemetry", "url": url}])}


def _with_recorder(cfg: dict) -> None:
    url = "inproc://recorder"
    register_inproc_server(url, build_server(TelemetryService(
        {"simulation": {"enabled": True}, "test_recorder": {"enabled": True}})))
    cfg["telemetry"] = {"domains": _all_domains([{"name": "telemetry", "url": url}])}


@pytest.fixture
def cfg(cfg, request):
    """Override the conftest cfg with a telemetry section chosen per test."""
    configure = getattr(request, "param", None)
    if configure is not None:
        configure(cfg)
    return cfg


def _incident(scenario_id: int, **kw) -> IncidentRequest:
    return IncidentRequest(scenario_id=scenario_id, description="High error rate and throughput drop in us-east region",
                           region="us-east", severity="high", **kw)


def _finding(report, domain: str):
    return next(f for f in report.agent_findings if f.domain == domain)


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_endpoint_down], indirect=True)
async def test_one_of_two_endpoints_down_marks_finding_degraded_but_keeps_results(engine):
    report = await engine.run_incident(_incident(1))

    metrics = _finding(report, "metrics")
    assert metrics.anomaly_detected  # data from the healthy endpoint was still analyzed
    assert "switch=C" in metrics.summary
    assert metrics.data_degraded  # ...but the missing endpoint is not hidden
    assert report.data_degraded
    assert not any(f.data_degraded for f in report.agent_findings if f.domain != "metrics")


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_scenario_outage], indirect=True)
async def test_concurrent_incidents_do_not_share_coverage_or_degradation(engine):
    healthy, broken = await asyncio.gather(
        engine.run_incident(_incident(1)),
        engine.run_incident(_incident(2)),
    )

    assert not _finding(healthy, "metrics").data_degraded
    assert not healthy.data_degraded
    assert _finding(broken, "metrics").data_degraded
    assert broken.data_degraded
    # The outage only affects the metrics source; other domains of the broken incident stay clean.
    assert not any(f.data_degraded for f in broken.agent_findings if f.domain != "metrics")


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_recorder], indirect=True)
async def test_query_window_is_anchored_to_reported_at(engine):
    RECORDED_QUERIES.clear()
    alarm = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    # Ingested much later than the alarm fired: the window must still end at the alarm.
    report = await engine.run_incident(_incident(1, reported_at=alarm))

    domains = {d for d, _ in RECORDED_QUERIES}
    assert domains == {"metrics", "logs", "routing", "config"}
    analysis = [q for _, q in RECORDED_QUERIES if q.start_time < alarm]
    status = [q for _, q in RECORDED_QUERIES if q.start_time == alarm]
    assert {q.end_time for q in analysis} == {alarm}
    assert {q.start_time for q in analysis} == {alarm - timedelta(minutes=30)}
    assert all(q.scenario_id == 1 and q.anchor_time == alarm for _, q in RECORDED_QUERIES)
    # The alarm is old, so each agent also asked whether it is still happening:
    # the 15 minutes after the alarm, never "until now".
    assert {q.end_time for q in status} == {alarm + timedelta(minutes=15)}
    # Simulation data is rebased onto that window, so the analysis still sees the incident.
    metrics = _finding(report, "metrics")
    assert metrics.anomaly_detected
    assert alarm - timedelta(minutes=30) <= metrics.start_time <= alarm


def test_normalizer_keeps_caller_supplied_alarm_time():
    incident = normalize_incident({"description": "x", "region": "us-east-1", "severity": "high",
                                   "reported_at": "2026-09-14T10:00:00Z"})
    assert incident.reported_at == datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)

    cloudwatch_style = normalize_incident({"description": "x", "region": "us-east-1", "severity": "high",
                                           "StateChangeTime": "2026-09-14T10:00:00.000+0000"})
    assert cloudwatch_style.reported_at == datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)

    naive = normalize_incident({"description": "x", "region": "us-east", "severity": "low",
                                "alarm_time": "2026-09-14T10:00:00"})
    assert naive.reported_at.tzinfo is not None and naive.reported_at.hour == 10

    before = datetime.now(timezone.utc)
    defaulted = normalize_incident({"description": "x", "region": "us-east", "severity": "low"})
    assert defaulted.reported_at >= before


def test_agent_query_falls_back_to_window_minutes_for_direct_callers():
    query = query_from_request({"region": "us-east", "window_minutes": 15, "scenario_id": 3})
    assert query.end_time - query.start_time == timedelta(minutes=15)
    assert datetime.now(timezone.utc) - query.end_time < timedelta(seconds=5)
