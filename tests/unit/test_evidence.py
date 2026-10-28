"""Detection output used by collaboration: full evidence, deduplicated key
events with causes first, scoped answers, live status and the new rule options."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

from contract import Signal, TelemetryQuery, TimeWindow, ToolResponse
from contract.catalog_loader import get_catalog
from contract.catalog_models import RateChangeRule, ThresholdRule
from detection import analyze_domain
from detection.rules import RuleContext, rate_change, series_key, threshold
from evals.build_fixtures import ALARM, NBR1, TUN2
from mcp_telemetry_server.plugins.simulation import SimulationPlugin
from simulation.scenarios import SCENARIOS

CAT = get_catalog()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cross_domain"


def _signals(name: str, domain: str) -> list[Signal]:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return [Signal.model_validate(s) for s in data["signals"] if s["domain"] == domain]


def _response(signals: list[Signal], minutes: int = 15) -> ToolResponse:
    return ToolResponse(window=TimeWindow(start=ALARM - timedelta(minutes=minutes), end=ALARM),
                        signals=[s for s in signals if ALARM - timedelta(minutes=minutes) <= s.timestamp <= ALARM])


def test_repeated_lines_collapse_and_the_cause_comes_first():
    finding = analyze_domain(CAT, "log", _response(_signals("asn-mismatch", "logs")), region="us-east-1")
    fired = [r["signal"] for e in finding.key_events for r in e.get("rules_fired", [])]
    assert fired[0] == "bgp_open_error"  # the 16 AWS BGP DOWN lines no longer crowd it out
    repeated = [e for e in finding.key_events if e.get("count")]
    assert repeated and repeated[0]["count"] == 16
    assert len(finding.key_events) <= CAT.domain("log").key_events.limit
    signals = {s for row in finding.evidence for s in row["signals"]}
    assert {"bgp_open_error", "bgp_neighbor_down", "vpn_bgp_status_down"} <= signals
    assert finding.evidence[0]["role"] == "cause" and finding.evidence[0]["entity_id"] == NBR1


def test_scoped_answer_skips_the_incident_text_filter():
    plugin = SimulationPlugin({})
    query = TelemetryQuery(region="us-east", start_time=ALARM - timedelta(minutes=30), end_time=ALARM, scenario_id=1)
    signals, _ = asyncio.run(plugin.fetch(query, domain="config"))
    response = ToolResponse(window=query.window, signals=signals)
    unrelated = "Users report slow logins in the north office"
    assert not analyze_domain(CAT, "config", response, region="us-east", incident_description=unrelated).anomaly_detected
    assert analyze_domain(CAT, "config", response, region="us-east", incident_description=unrelated, scoped=True).anomaly_detected
    assert SCENARIOS[1].config_changes  # the change exists; only relevance hid it


def test_flapping_tunnel_reports_episodes_and_current_state():
    signals = _signals("tunnel-flapping", "metrics")
    before = [s for s in signals if s.timestamp <= ALARM]
    after = [s for s in signals if s.timestamp > ALARM]
    finding = analyze_domain(CAT, "metrics", _response(before), region="us-east-1", after=after)
    tunnel = next(r for r in finding.entity_status if r["entity_id"] == TUN2)
    assert tunnel["episodes"] >= 2 and tunnel["current_state"] in ("firing", "cleared")


def test_recovered_entity_is_cleared_after_the_alarm():
    signals = _signals("bgp-self-recovered", "metrics")
    finding = analyze_domain(CAT, "metrics", _response([s for s in signals if s.timestamp <= ALARM]),
                             region="us-east-1", after=[s for s in signals if s.timestamp > ALARM])
    neighbor = next(r for r in finding.entity_status if r["entity_type"] == "bgp_neighbor")
    assert neighbor == {**neighbor, "current_state": "cleared", "episodes": 1}


def _metric(name: str, value: float, **labels: str) -> Signal:
    return Signal(signal_type="metric", timestamp=ALARM, observed_at=ALARM, entity={"type": "tunnel", "id": "t1", "labels": labels},
                  name=name, value=value, domain="metrics", source="test")


def test_threshold_per_capacity_and_scale():
    rule = ThresholdRule(type="threshold", op="gt", value=0.8, per="labels.capacity_bps")
    assert threshold(rule, _metric("link_out_bps", 1.1e9, capacity_bps="1250000000"), "r")
    assert threshold(rule, _metric("link_out_bps", 0.5e9, capacity_bps="1250000000"), "r") is None
    assert threshold(rule, _metric("link_out_bps", 1.1e9), "r") is None  # no capacity: no verdict
    scaled = ThresholdRule(type="threshold", op="gt", value=100, scale=8)
    assert threshold(scaled, _metric("x", 13), "r")


def test_rate_change_median_ignores_a_burst():
    ctx = RuleContext(region="us-east-1")
    history = [_metric("TunnelDataIn", v).model_copy(update={"timestamp": ALARM - timedelta(minutes=i + 1)})
               for i, v in enumerate([1000, 1000, 1000, 50000])]
    for s in history:
        ctx.series.setdefault(series_key(s), []).append(s)
    now = _metric("TunnelDataIn", 900)
    mean_rule = RateChangeRule(type="rate_change", direction="decrease", factor=10, min_reference=100)
    median_rule = RateChangeRule(type="rate_change", direction="decrease", factor=10, min_reference=100, reference="median")
    assert rate_change(mean_rule, now, ctx, "r") is not None  # the one burst drags the mean to 13250
    assert rate_change(median_rule, now, ctx, "r") is None  # 900 vs median 1000 is not a collapse
