"""Each detection rule type, plus templates, relevance and correlation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contract import Entity, Signal
from contract.catalog_loader import load_catalog
from contract.catalog_models import (
    EventPresenceRule,
    RateChangeRule,
    RelevanceSpec,
    StateEqualsRule,
    ThresholdRule,
    ZScoreBaselineRule,
)
from detection import reconsider
from detection.relevance import is_relevant
from detection.rules import RuleContext, evaluate, series_key
from detection.templates import render
from models.schemas import AgentFinding, EntityBaseline

T0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def sig(name="m", value=1.0, minutes=0, severity="info", signal_type="metric", attributes=None, labels=None,
        entity=("tunnel", "t1")) -> Signal:
    ts = T0 - timedelta(minutes=minutes)
    return Signal(signal_type=signal_type, timestamp=ts, observed_at=ts, name=name, value=value, severity=severity,
                  entity=Entity(type=entity[0], id=entity[1], labels=labels or {}), domain="metrics", source="test",
                  attributes=attributes or {})


def run(rule, signal, ctx=None, names=None):
    return evaluate(rule, signal, ctx or RuleContext(region="us-east-1"), rule_id="r", catalog_names=names or [signal.name])


# ----------------------------------------------------------------- threshold

@pytest.mark.parametrize("op,kw,value,fires", [
    ("gt", {"value": 2}, 2.5, True), ("gt", {"value": 2}, 2.0, False),
    ("lt", {"value": 0.7}, 0.5, True), ("lt", {"value": 0.7}, 0.7, False),
    ("between", {"min": 10, "max": 20}, 15, True), ("between", {"min": 10, "max": 20}, 25, False),
])
def test_threshold(op, kw, value, fires):
    hit = run(ThresholdRule(type="threshold", op=op, **kw), sig(value=value))
    assert (hit is not None) == fires


def test_threshold_on_attribute_and_non_numeric_value():
    rule = ThresholdRule(type="threshold", op="gt", value=0, field="attributes.drops", reason="drops seen")
    assert run(rule, sig(value=None, attributes={"drops": 12})).reason == "drops seen"
    assert run(rule, sig(value=None, attributes={"drops": "n/a"})) is None


# -------------------------------------------------------------- state_equals

def test_state_equals_numeric_value_and_string_attribute():
    assert run(StateEqualsRule(type="state_equals", value=0), sig(value=0.0)) is not None
    assert run(StateEqualsRule(type="state_equals", value=0), sig(value=1.0)) is None
    rule = StateEqualsRule(type="state_equals", field="attributes.state", value="blackhole")
    assert run(rule, sig(value=None, attributes={"state": "blackhole"})) is not None
    assert run(rule, sig(value=None, attributes={"state": "active"})) is None


# ------------------------------------------------------------ zscore_baseline

def _baseline(mean, std):
    return EntityBaseline(entity_key="k", metric="m", mean=mean, std_dev=std, sample_count=100, last_updated=T0)


def test_zscore_uses_baseline_window_samples_first():
    rule = ZScoreBaselineRule(type="zscore_baseline", threshold=3.0)
    current = sig(value=50.0)
    history = [sig(value=v, minutes=60 + i) for i, v in enumerate([10, 11, 9, 10, 10])]
    ctx = RuleContext(region="us-east-1", baseline_series={series_key(current): history},
                      baseline_lookup=lambda k, m: pytest.fail("provider must not be consulted"))
    hit = run(rule, current, ctx)
    assert hit is not None and hit.z > 3 and hit.reason.startswith("m z=")


def test_zscore_falls_back_to_baseline_provider_in_key_order():
    rule = ZScoreBaselineRule(type="zscore_baseline", threshold=3.0,
                              baseline_keys=["switch=label:switch", "region=region"])
    seen = []

    def lookup(key, metric):
        seen.append(key)
        return _baseline(1.0, 0.1) if key == "region:us-east-1" else None

    hit = run(rule, sig(value=2.0, labels={"switch": "C"}), RuleContext(region="us-east-1", baseline_lookup=lookup))
    assert seen == ["switch:C", "region:us-east-1"]
    assert hit.z == pytest.approx(10.0)


def test_zscore_threshold_fallback_only_without_any_baseline_and_when_enabled():
    rule = ZScoreBaselineRule(type="zscore_baseline", threshold=3.0,
                              fallback={"type": "threshold", "op": "gt", "value": 2, "reason": "legacy-threshold"})
    none = RuleContext(region="r", baseline_lookup=lambda k, m: None)
    assert run(rule, sig(value=5.0), none).reason == "legacy-threshold"
    assert run(rule, sig(value=5.0), RuleContext(region="r", baseline_lookup=lambda k, m: None, legacy_fallback=False)) is None
    # A found baseline blocks the fallback even when it doesn't fire (zero variance included).
    assert run(rule, sig(value=5.0), RuleContext(region="r", baseline_lookup=lambda k, m: _baseline(5.0, 0.0))) is None


# --------------------------------------------------------------- rate_change

def test_rate_change_increase_and_decrease_against_earlier_samples():
    up = RateChangeRule(type="rate_change", direction="increase", factor=5, min_reference=1)
    series = [sig(value=5.0, minutes=m) for m in (10, 8, 6)]
    spike = sig(value=140.0, minutes=2)
    ctx = RuleContext(region="r", series={series_key(spike): [*series, spike]})
    assert run(up, spike, ctx) is not None
    assert run(up, series[-1], ctx) is None                     # 5 vs 5: steady
    assert run(up, series[0], ctx) is None                      # no earlier reference

    down = RateChangeRule(type="rate_change", direction="decrease", factor=10, min_reference=1000)
    drop = sig(value=0.0, minutes=1)
    ctx = RuleContext(region="r", series={series_key(drop): [sig(value=52000.0, minutes=5), drop]})
    assert run(down, drop, ctx) is not None
    tiny = RuleContext(region="r", series={series_key(drop): [sig(value=500.0, minutes=5), drop]})
    assert run(down, drop, tiny) is None                        # reference below min_reference


# ------------------------------------------------------------ event_presence

def test_event_presence_allow_list_and_min_severity():
    rule = EventPresenceRule(type="event_presence", names=["RevokeSecurityGroupIngress"], min_severity="info")
    assert run(rule, sig(name="RevokeSecurityGroupIngress", signal_type="event", value=None)) is not None
    assert run(rule, sig(name="DescribeSecurityGroups", signal_type="event", value=None)) is None
    errors_only = EventPresenceRule(type="event_presence", min_severity="error")
    assert run(errors_only, sig(name="log", severity="critical", value=None)) is not None
    assert run(errors_only, sig(name="log", severity="warning", value=None)) is None


# ------------------------------------------------------------ templates (safe)

def test_templates_resolve_dotted_keys_only_and_never_raise():
    ctx = {"worst": {"tags": {"switch": "C"}, "error_rate": 2.5}, "hit_count": 1}
    assert render("{hit_count} on {worst.tags.switch} {worst.error_rate:.2f}", ctx) == "1 on C 2.50"
    assert render("{worst.tags.core} {worst.packet_loss:.2f}", ctx) == "unknown unknown"
    with pytest.raises(ValueError):
        render("{worst[tags]}", ctx)
    assert render("{worst.__class__}", ctx) == "unknown"


# ----------------------------------------------------------------- relevance

def test_incident_text_relevance():
    spec = load_catalog().domain("config").relevance
    change = sig(name="config_change", signal_type="event", value=None, entity=("component", "NTP"),
                 attributes={"change_type": "policy_update", "before": {"server": "10.0.0.1"}, "after": {"server": "pool.ntp.org"}})
    assert is_relevant(spec, change, "")                                       # no description
    assert is_relevant(spec, change, "x")                                      # no usable tokens
    assert not is_relevant(spec, change, "Distributed auth failures")          # no overlap, no symptom
    assert is_relevant(spec, change, "NTP server drift")                       # token overlap
    assert is_relevant(spec, change, "high packet loss in us-east")            # symptom + policy_update
    assert isinstance(spec, RelevanceSpec)


# --------------------------------------------------------------- correlation

def _finding(domain, anomaly, summary="s", key_events=None, confidence=0.5):
    return AgentFinding(agent_id=domain, domain=domain, anomaly_detected=anomaly, summary=summary,
                        key_events=key_events or [], start_time=T0, end_time=T0, confidence=confidence)


def test_correlation_rules_apply_once_in_priority_order():
    cat = load_catalog()
    metrics = _finding("metrics", True, key_events=[{"throughput_gbps": 0.3}], confidence=0.68)
    routing = _finding("routing", True, summary="change=reroute", key_events=[{"change_type": "reroute"}], confidence=0.8)

    m2 = reconsider(metrics, [routing], cat)
    assert m2.confidence == 0.92 and m2.revision_count == 1
    assert m2.summary.endswith("Peer corroboration: routing agent reported reroute during throughput drop; metrics confidence elevated to 0.92")
    assert reconsider(m2, [routing], cat).summary == m2.summary  # idempotent

    r2 = reconsider(routing, [metrics], cat)                      # first rule (reroute + throughput) wins
    assert r2.confidence == 0.91 and "metrics reported throughput drop" in r2.summary

    quiet_log = reconsider(_finding("log", False, summary="No log anomaly", confidence=0.2), [metrics, routing], cat)
    assert quiet_log.confidence == 0.74
    assert quiet_log.summary == "No log anomaly. Peer contradiction: no direct log error pattern while [metrics, routing] reported anomalies"

    alone = _finding("config", False, confidence=0.2)
    assert reconsider(alone, [_finding("log", False)], cat) == alone   # no peer anomaly: unchanged
