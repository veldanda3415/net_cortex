"""LLM proposes, agents verify: the pieces around a hypothesis.

The rule set stays closed, the four code checks decide, unclassified rows
carry their text (deduplicated), the logs plugin can pass unmatched lines
through without changing what it classifies, collaboration only skips when
the LLM has nothing to read, and supported rules become draft catalog
entries."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

from agents import triage
from contract import Coverage, Entity, Signal, TelemetryQuery, ToolResponse
from contract.catalog_loader import load_catalog
from contract.catalog_models import SignalDef
from core.orchestrator import NetCortexEngine, collect_hypotheses
from detection import analyze_domain, hypotheses
from mcp_telemetry_server.plugins.aws.logs import CloudWatchLogsPlugin
from models.schemas import A2AMessage, AgentFinding

CATALOG = load_catalog()
NOW = datetime(2026, 10, 28, 5, 32, tzinfo=timezone.utc)
REGION = "us-east-1"

TOPOLOGY = {
    "region": REGION,
    "entities": [{"type": "flow", "id": "onprem-to-nlb"}, {"type": "load_balancer", "id": "lb-app"},
                 {"type": "security_group", "id": "sg-app"}, {"type": "security_group", "id": "sg-other"},
                 {"type": "instance", "id": "i-other"}],
    "relationships": [
        {"type": "probes", "source": {"type": "flow", "id": "onprem-to-nlb"}, "target": {"type": "load_balancer", "id": "lb-app"}},
        {"type": "protected_by", "source": {"type": "load_balancer", "id": "lb-app"}, "target": {"type": "security_group", "id": "sg-app"}},
        {"type": "protected_by", "source": {"type": "instance", "id": "i-other"}, "target": {"type": "security_group", "id": "sg-other"}},
    ],
}
ALARM = [{"type": "flow", "id": "onprem-to-nlb"}]


def _event(minute: float, name="AssociateRouteTable", entity=("security_group", "sg-app"), ref=None, **attrs) -> Signal:
    t = NOW + timedelta(minutes=minute)
    return Signal(signal_type="event", timestamp=t, observed_at=t, entity=Entity(type=entity[0], id=entity[1]),
                  name=name, severity="info", domain="config", source="cloudtrail",
                  evidence_ref=ref or f"cloudtrail:{name}-{minute}", attributes={"region": REGION, **attrs})


def _finding(domain="config", **kw) -> AgentFinding:
    return AgentFinding(agent_id=domain, domain=domain, anomaly_detected=False, summary="clean", key_events=[],
                        start_time=NOW, end_time=NOW, confidence=0.1, **kw)


# ----------------------------------------------------------------- the rule set

@pytest.mark.parametrize("rule", [
    {"type": "regex", "pattern": ".*"},                                    # not a rule type
    {"type": "threshold", "op": "gt"},                                     # no value
    {"type": "event_presence", "min_severity": "loud"},
    {"type": "zscore_baseline", "threshold": 3},                           # needs a baseline store
    {"type": "event_presence", "eval": "__import__('os')"},                # extra keys are refused
    "event_presence",
])
def test_a_hypothesis_rule_must_come_from_the_closed_set(rule):
    with pytest.raises(ValueError):
        hypotheses.parse_rule(rule)


def test_rule_ids_are_llm_prefixed_and_stable():
    rule = hypotheses.parse_rule({"type": "event_presence", "min_severity": "info"})
    a = hypotheses.rule_hash("X", {"type": "t", "id": "1"}, rule)
    assert a.startswith("llm:") and a == hypotheses.rule_hash("X", {"type": "t", "id": "1"}, rule)
    assert a != hypotheses.rule_hash("X", {"type": "t", "id": "2"}, rule)


# --------------------------------------------------------------------- checks

def test_fire_only_counts_the_named_signal_on_the_entity():
    rule = hypotheses.parse_rule({"type": "event_presence"})
    signals = [_event(-3), _event(-2, entity=("security_group", "sg-other")), _event(-1, name="CreateTags")]
    hits = hypotheses.fire(rule, "llm:x", "AssociateRouteTable", signals, {("security_group", "sg-app")}, REGION)
    assert [s.evidence_ref for s, _ in hits] == ["cloudtrail:AssociateRouteTable--3"]


def test_metric_threshold_hypothesis_fires_on_the_bad_samples():
    rule = hypotheses.parse_rule({"type": "threshold", "op": "lt", "value": 1})
    samples = [Signal(signal_type="metric", timestamp=NOW + timedelta(minutes=m), observed_at=NOW,
                      entity=Entity(type="bgp_neighbor", id="169.254.100.1"), name="PrefixesSent", value=v,
                      domain="metrics", source="cloudwatch", evidence_ref=f"cw:{m}")
               for m, v in [(-4, 1.0), (-3, 1.0), (-2, 0.0), (-1, 0.0)]]
    hits = hypotheses.fire(rule, "llm:x", "PrefixesSent", samples, {("bgp_neighbor", "169.254.100.1")}, REGION)
    assert [s.evidence_ref for s, _ in hits] == ["cw:-2", "cw:-1"]


def test_the_earlier_window_is_as_long_and_ends_before_the_lookback():
    start, end = NOW - timedelta(minutes=15), NOW
    a, b = hypotheses.window_before(start, end, 15)
    assert (b, b - a) == (start - timedelta(minutes=15), timedelta(minutes=15))


def test_placed_means_on_or_upstream_of_the_alarm():
    assert hypotheses.placed({"type": "security_group", "id": "sg-app"}, TOPOLOGY, CATALOG, ALARM, [])
    assert hypotheses.placed({"type": "flow", "id": "onprem-to-nlb"}, TOPOLOGY, CATALOG, ALARM, [])
    assert not hypotheses.placed({"type": "security_group", "id": "sg-other"}, TOPOLOGY, CATALOG, ALARM, [])
    assert not hypotheses.placed({"type": "security_group", "id": "sg-unknown"}, TOPOLOGY, CATALOG, ALARM, [])
    assert not hypotheses.placed({"type": "security_group", "id": "sg-app"}, None, CATALOG, ALARM, [])
    # no alarm and no symptoms: nothing to be upstream of
    assert not hypotheses.placed({"type": "security_group", "id": "sg-app"}, TOPOLOGY, CATALOG, [], [])


def test_a_supported_hypothesis_is_evidence_with_no_role():
    h = {"signal": "AssociateRouteTable", "entity": {"type": "security_group", "id": "sg-app"}, "rule_id": "llm:abc",
         "why": "because"}
    rule = hypotheses.parse_rule({"type": "event_presence"})
    hits = hypotheses.fire(rule, "llm:abc", h["signal"], [_event(-3)], {("security_group", "sg-app")}, REGION)
    row = hypotheses.evidence_row(h, hits)
    assert row["via"] == "hypothesis" and row["role"] is None
    assert row["rules_fired"][0]["rule"] == "llm:abc" and row["evidence_refs"] == ["cloudtrail:AssociateRouteTable--3"]


# --------------------------------------------------------------------- triage

def _unclassified_finding() -> AgentFinding:
    response = ToolResponse(window={"start": NOW - timedelta(minutes=15), "end": NOW},
                            coverage=[Coverage(source="x", status="ok")],
                            signals=[_event(-3, ref="r1", request='{"routeTableId": "rtb-1"}'),
                                     _event(-2, name="CreateTags", entity=("instance", "i-other"), ref="r2")])
    return analyze_domain(CATALOG, "config", response, region=REGION)


def test_triage_keeps_only_grounded_single_entity_closed_set_hypotheses():
    own = _unclassified_finding()
    ctx = {"start_time": (NOW - timedelta(minutes=15)).isoformat(), "end_time": NOW.isoformat()}
    raw = {"hypotheses": [
        {"evidence_refs": ["r1"], "signal": "AssociateRouteTable", "rule": {"type": "event_presence"}, "why": "w",
         "role": "cause", "proposed_name": "route_table_associated"},
        {"evidence_refs": ["nope"], "signal": "AssociateRouteTable", "rule": {"type": "event_presence"}},
        {"evidence_refs": ["r1", "r2"], "signal": "AssociateRouteTable", "rule": {"type": "event_presence"}},
        {"evidence_refs": ["r1"], "signal": "AssociateRouteTable", "rule": {"type": "python", "code": "1"}},
    ]}
    requests = triage.to_requests(raw, "config", own, ctx, TOPOLOGY, CATALOG)
    assert len(requests) == 1
    r = requests[0]
    assert r["target"] == r["sender"] == "config" and r["hops"] == 0
    assert r["entity"] == {"type": "security_group", "id": "sg-app"}
    assert r["hypothesis"]["rule_id"].startswith("llm:") and r["hypothesis"]["signal"] == "AssociateRouteTable"
    assert triage.to_requests("not json", "config", own, ctx, TOPOLOGY, CATALOG) == []


def test_triage_prompt_shows_the_line_text():
    own = _unclassified_finding()
    prompt = triage.build_prompt("config", own, [], {"incident_description": "flows failing", "alarm_entities": ALARM})
    assert 'rtb-1' in prompt and "refs=[r1]" in prompt and "flow:onprem-to-nlb" in prompt


def test_llm_is_off_without_credentials_or_when_disabled(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    assert not triage.llm_enabled({})
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert not triage.llm_enabled({"llm": {"hypotheses": False}})


# ------------------------------------------------------- unclassified evidence

def test_unclassified_rows_carry_text_and_collapse_repeats():
    line = lambda m, n: Signal(signal_type="event", timestamp=NOW + timedelta(minutes=m), observed_at=NOW,
                               entity=Entity(type="bgp_neighbor", id="169.254.100.1"), name="frr_unmatched",
                               severity="info", domain="logs", source="cloudwatch_logs", evidence_ref=f"l{m}",
                               attributes={"message": f"bgpd[{n}]: %MAXPFX: neighbor 169.254.100.1 reached 2, max 1"})
    other = Signal(signal_type="event", timestamp=NOW, observed_at=NOW, entity=Entity(type="bgp_neighbor", id="169.254.100.1"),
                   name="frr_unmatched", severity="warning", domain="logs", source="cloudwatch_logs", evidence_ref="o",
                   attributes={"message": "bgpd: %NOTIFICATION: sent to neighbor 169.254.100.1 6/1 (Cease/Maximum Number of Prefixes Reached)"})
    response = ToolResponse(window={"start": NOW - timedelta(minutes=15), "end": NOW + timedelta(minutes=5)},
                            coverage=[Coverage(source="x", status="ok")], signals=[line(-3, 812), line(-2, 813), other])
    rows = analyze_domain(CATALOG, "log", response, region=REGION).unclassified_evidence
    assert [r["count"] for r in rows] == [1, 2]                     # the warning first, the repeat collapsed
    assert "Cease/Maximum Number of Prefixes" in rows[0]["text"]
    assert rows[1]["evidence_refs"] == ["l-3", "l-2"] and rows[1]["last_seen"] > rows[1]["timestamp"]


# --------------------------------------------------------- logs passthrough

END = datetime.now(timezone.utc).replace(second=0, microsecond=0)
QUERY = TelemetryQuery(region=REGION, start_time=END - timedelta(minutes=30), end_time=END)


def _log(minutes_ago: float, message: str, eid: str) -> dict:
    return {"logStreamName": "i-0router", "timestamp": int((END - timedelta(minutes=minutes_ago)).timestamp() * 1000),
            "message": message, "eventId": eid}


FRR = {
    "id": "frr", "log_group": "/lab/router/frr", "filter_pattern": "?ADJCHANGE ?NOTIFICATION",
    "entity": {"type": "bgp_neighbor"},
    "classify": [{"name": "bgp_neighbor_down", "pattern": r"ADJCHANGE: neighbor (?P<id>[0-9.]+)\S* .*\bDown\b"}],
    "unmatched": {"name": "frr_unmatched", "entity": {"type": "bgp_neighbor", "field": "@message",
                                                      "pattern": r"\b(?P<id>\d{1,3}(?:\.\d{1,3}){3})\b"}},
}
DOWN = "bgpd: %ADJCHANGE: neighbor 169.254.10.1(Unknown) in vrf default Down Maximum prefix received"
NOTE = "bgpd: %NOTIFICATION: sent to neighbor 169.254.10.1 6/1 (Cease/Maximum Number of Prefixes Reached) 7 bytes"
MAXPFX = "bgpd: %MAXPFX: No. of IPv4 Unicast prefix received from 169.254.10.1 reaches 5, max 1"
NOISE = "zebra: interface ens5 index 2 up"


def _stub(plugin) -> Stubber:
    client = boto3.client("logs", region_name=REGION, aws_access_key_id="x", aws_secret_access_key="x")
    plugin._clients[("logs", REGION)] = client
    stub = Stubber(client)
    stub.activate()
    return stub


@pytest.mark.asyncio
async def test_unmatched_lines_pass_through_without_changing_the_classified_ones():
    plugin = CloudWatchLogsPlugin({"queries": [FRR]})
    stub = _stub(plugin)
    stub.add_response("filter_log_events", {"events": [_log(5, NOTE, "e2"), _log(4, DOWN, "e3")]},
                      {"logGroupName": "/lab/router/frr", "startTime": ANY, "endTime": ANY,
                       "filterPattern": "?ADJCHANGE ?NOTIFICATION"})
    stub.add_response("filter_log_events", {"events": [_log(6, MAXPFX, "e1"), _log(5, NOTE, "e2"), _log(4, DOWN, "e3"),
                                                       _log(3, NOISE, "e4")]},
                      {"logGroupName": "/lab/router/frr", "startTime": ANY, "endTime": ANY})
    signals, coverage = await plugin.fetch(QUERY, domain="logs")
    assert coverage.status == "ok"
    by_name = {}
    for s in signals:
        by_name.setdefault(s.name, []).append(s)
    assert [s.attributes["message"] for s in by_name["bgp_neighbor_down"]] == [DOWN]          # unchanged
    unmatched = by_name["frr_unmatched"]
    assert [s.attributes["message"] for s in unmatched] == [MAXPFX, NOTE, NOISE]
    assert [s.entity.id for s in unmatched] == ["169.254.10.1", "169.254.10.1", "i-0router"]   # neighbor, else stream
    # what the catalog does with them: nothing fires, they are unclassified evidence
    finding = analyze_domain(CATALOG, "log", ToolResponse(window=QUERY.window, coverage=[coverage], signals=signals),
                             region=REGION, incident_description="BGP down")
    assert {r["name"] for r in finding.unclassified_evidence} == {"frr_unmatched"}
    assert {r["signal"] for e in finding.key_events for r in e.get("rules_fired", [])} == {"bgp_neighbor_down"}


@pytest.mark.asyncio
async def test_without_a_filter_pattern_the_group_is_read_once():
    query = {**FRR, "filter_pattern": ""}
    plugin = CloudWatchLogsPlugin({"queries": [query]})
    stub = _stub(plugin)
    stub.add_response("filter_log_events", {"events": [_log(6, MAXPFX, "e1"), _log(4, DOWN, "e3")]},
                      {"logGroupName": "/lab/router/frr", "startTime": ANY, "endTime": ANY})
    signals, _ = await plugin.fetch(QUERY, domain="logs")
    assert sorted(s.name for s in signals) == ["bgp_neighbor_down", "frr_unmatched"]
    stub.assert_no_pending_responses()


def test_unmatched_needs_unaggregated_lines():
    with pytest.raises(ValueError):
        CloudWatchLogsPlugin({"queries": [{**FRR, "aggregate": "count_per_minute"}]})


def test_aws_lab_passthrough_is_on_for_the_quiet_groups_only():
    import yaml

    cfg = yaml.safe_load(open("config/telemetry.aws-lab.yaml"))["sources"]["cloudwatch_logs"]
    on = {q["id"] for q in cfg["queries"] if q.get("unmatched")}
    assert on == {"frr", "charon", "vpn-bgp", "network-manager"}
    CloudWatchLogsPlugin(cfg)


# --------------------------------------------------------------- short-circuit

def test_collaboration_skips_only_when_the_llm_has_nothing_to_read():
    clean = [_finding("log"), _finding("config")]
    with_text = [_finding("log"), _finding("config", unclassified_evidence=[{"name": "x"}])]
    assert NetCortexEngine.should_short_circuit(clean)
    assert NetCortexEngine.should_short_circuit(with_text)                     # LLM off: unchanged
    assert NetCortexEngine.should_short_circuit(clean, llm_on=True)            # nothing to read, no alarm
    assert not NetCortexEngine.should_short_circuit(with_text, llm_on=True)
    assert not NetCortexEngine.should_short_circuit(clean, llm_on=True, alarm=True)


# ------------------------------------------------------------ report + drafts

def test_report_lists_every_hypothesis_with_its_verdict():
    h = {"rule_id": "llm:1", "signal": "X", "entity": {"type": "t", "id": "1"}, "rule": {"type": "event_presence"}}
    messages = [
        A2AMessage(sender_agent="config", target_agent="config", message_type="validation_request", round_number=1,
                   payload={"hypothesis": h, "response": {"verdict": "refuted",
                                                          "checks": {"grounded": True, "fires": True, "new": False,
                                                                     "placed": True}}}),
        A2AMessage(sender_agent="log", target_agent="config", message_type="validation_request", round_number=1,
                   payload={"entity": {"type": "t", "id": "1"}, "response": {"verdict": "supported"}}),
    ]
    out = collect_hypotheses(messages)
    assert len(out) == 1 and out[0]["verdict"] == "refuted" and out[0]["checks"]["new"] is False


def test_supported_hypotheses_become_loadable_draft_catalog_entries():
    supported = {"verdict": "supported", "id": "1", "rule_id": "llm:1", "domain": "config", "signal": "AssociateRouteTable",
                 "signal_type": "event", "entity": {"type": "route_table", "id": "rtb-1"},
                 "rule": {"type": "event_presence", "min_severity": "info"}, "proposed_name": "Route Table Associated",
                 "description": "a subnet moved to another route table", "checks": {c: True for c in hypotheses.CHECKS}}
    refuted = {**supported, "verdict": "refuted", "proposed_name": "nope"}
    drafts = hypotheses.draft_entries([supported, refuted], CATALOG)
    assert list(drafts["signals"]) == ["route_table_associated"]
    entry = drafts["signals"]["route_table_associated"]
    SignalDef.model_validate(entry)
    assert entry["match_names"] == ["AssociateRouteTable"] and "human approval" in drafts["review"]["route_table_associated"]["status"]


def test_a_suggested_classify_pattern_is_kept_only_if_it_matches_the_lines():
    lines = [MAXPFX]
    assert hypotheses.verify_pattern(r"%MAXPFX: .* reaches \d+, max \d+", lines)
    assert hypotheses.verify_pattern(r"Bad Peer AS", lines) is None
    assert hypotheses.verify_pattern(r"([", lines) is None


def test_a_catalog_override_entry_set_to_null_deletes_it(tmp_path):
    (tmp_path / "signals.json").write_text(json.dumps({"signals": {"PrefixesSent": None}}))
    (tmp_path / "investigation.json").write_text(json.dumps({"signal_roles": {"PrefixesSent": None}}))
    cat = load_catalog(tmp_path)
    assert cat.signal("PrefixesSent") is None and cat.signal_role("PrefixesSent") is None
    assert cat.signal("BgpPeerState") is not None


def test_a_hypothesis_on_an_older_line_checks_from_that_line():
    """A scoped answer can turn up a change older than the incident window (its
    question reached back by the lookback); the hypothesis window starts there."""
    response = ToolResponse(window={"start": NOW - timedelta(minutes=40), "end": NOW},
                            coverage=[Coverage(source="x", status="ok")], signals=[_event(-19, ref="old")])
    own = analyze_domain(CATALOG, "config", response, region=REGION)
    ctx = {"start_time": (NOW - timedelta(minutes=15)).isoformat(), "end_time": NOW.isoformat()}
    raw = {"hypotheses": [{"evidence_refs": ["old"], "signal": "AssociateRouteTable", "rule": {"type": "event_presence"}}]}
    [r] = triage.to_requests(raw, "config", own, ctx, TOPOLOGY, CATALOG)
    assert r["start_time"] == (NOW - timedelta(minutes=19)).isoformat() and r["end_time"] == NOW.isoformat()


@pytest.mark.parametrize("text,expected", [('[{"hypotheses": []}]', {"hypotheses": []}), ('[1, 2]', None), ('"x"', None)])
def test_an_llm_reply_that_is_not_one_object_is_unwrapped_or_dropped(monkeypatch, text, expected):
    import types

    from agents import rca_synthesizer

    class Models:
        def generate_content(self, **kw):
            return types.SimpleNamespace(text=text)

    genai = pytest.importorskip("google.genai")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(genai, "Client", lambda **kw: types.SimpleNamespace(models=Models()))
    assert rca_synthesizer._call_gemini("p", "m") == expected
