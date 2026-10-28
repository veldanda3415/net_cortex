"""Collaboration reasoning (detection/investigate.py): topology walk, the
questions agents ask, answers, ranking and incident status."""

from __future__ import annotations

from datetime import datetime, timedelta

from contract.catalog_loader import get_catalog
from detection.investigate import (
    TopologyGraph,
    absorb,
    find_leads,
    incident_status,
    plan_requests,
    rank_causes,
    verdict,
)
from evals.build_fixtures import ALARM, DNS_APP, RTB_SPOKE, TUN1, VPN, ZONE, topology
from models.schemas import AgentFinding

CAT = get_catalog()
TOPO = {"region": "us-east-1", **topology()}


def _finding(domain: str, rows: list[dict], *, anomaly: bool = True, key_events: list[dict] | None = None,
             degraded: bool = False) -> AgentFinding:
    return AgentFinding(agent_id=domain, domain=domain, anomaly_detected=anomaly, summary=f"{domain} summary",
                        key_events=key_events or [], start_time=ALARM - timedelta(minutes=3), end_time=ALARM,
                        confidence=0.8 if anomaly else 0.2, evidence=rows, data_degraded=degraded)


def _row(etype: str, eid: str, signals: list[str], minutes_before: float, role: str | None = None,
         severity: str = "warning") -> dict:
    t = (ALARM - timedelta(minutes=minutes_before)).isoformat()
    return {"entity_type": etype, "entity_id": eid, "signals": signals, "role": role, "severity": severity,
            "first_seen": t, "last_seen": t, "count": 1, "rules_fired": [], "evidence_refs": [f"ref:{eid}:{signals[0]}"]}


def test_alias_resolves_cloudwatch_tunnel_ip_to_topology_tunnel():
    graph = TopologyGraph.build(TOPO, CAT)
    assert graph.resolve("tunnel", TUN1) == ("tunnel", f"{VPN}-tunnel-1")
    assert graph.resolve("tunnel", "203.0.113.99") is None


def test_dns_name_depends_on_hosted_zone_not_the_reverse():
    graph = TopologyGraph.build(TOPO, CAT)
    assert graph.depends_on(("dns_name", DNS_APP), ("hosted_zone", ZONE), 3)
    assert not graph.depends_on(("hosted_zone", ZONE), ("dns_name", DNS_APP), 3)


def test_agent_asks_other_domains_about_what_its_anomaly_depends_on():
    log = _finding("log", [_row("dns_name", DNS_APP, ["dns_nxdomain"], 4, role="symptom")])
    requests = plan_requests("log", log, [], TOPO, CAT, window_start=ALARM - timedelta(minutes=15), window_end=ALARM)
    asked = {(r["target"], r["entity"]["type"], r["entity"]["id"]) for r in requests}
    assert ("config", "hosted_zone", ZONE) in asked
    assert all(r["target"] != "log" for r in requests)  # never asks itself
    zone = next(r for r in requests if r["entity"]["type"] == "hosted_zone")
    # The window reaches back lookback_minutes before the window start.
    assert datetime.fromisoformat(zone["start_time"]) == ALARM - timedelta(minutes=30)
    assert zone["path"] == [f"dns_name:{DNS_APP}", f"hosted_zone:{ZONE}"]


def test_no_question_when_the_peer_already_has_evidence_or_was_asked():
    log = _finding("log", [_row("dns_name", DNS_APP, ["dns_nxdomain"], 4, role="symptom")])
    config = _finding("config", [_row("hosted_zone", ZONE, ["ChangeResourceRecordSets"], 19, role="cause")])
    requests = plan_requests("log", log, [config], TOPO, CAT, window_start=ALARM - timedelta(minutes=15), window_end=ALARM)
    assert not any(r["entity"]["type"] == "hosted_zone" and r["target"] == "config" for r in requests)

    first = plan_requests("log", log, [], TOPO, CAT, window_start=ALARM - timedelta(minutes=15), window_end=ALARM)
    again = plan_requests("log", log, [], TOPO, CAT, window_start=ALARM - timedelta(minutes=15), window_end=ALARM,
                          asked=[r["request_id"] for r in first])
    assert again == []


def test_no_questions_without_topology_or_when_entities_are_unknown():
    sim = _finding("metrics", [_row("switch", "C", ["error_rate"], 3)])
    assert plan_requests("metrics", sim, [], None, CAT, window_start=ALARM, window_end=ALARM) == []
    assert plan_requests("metrics", sim, [], TOPO, CAT, window_start=ALARM, window_end=ALARM) == []


def test_alarm_entities_anchor_an_agent_that_can_see_them():
    requests = plan_requests("metrics", None, [], TOPO, CAT, window_start=ALARM - timedelta(minutes=15), window_end=ALARM,
                             alarm_entities=[{"type": "flow", "id": "svc-to-app"}])
    assert any(r["entity"]["type"] == "hosted_zone" for r in requests)


def test_verdicts_and_leads_from_a_scoped_answer():
    cloudtrail_event = {"timestamp": ALARM.isoformat(), "rules_fired": [{"signal": "DeleteRoute"}],
                        "request": '{"routeTableId": "rtb-0a7c3e5d9f1b2a4c6", "natGatewayId": "nat-0123456789abcdef0"}'}
    found = _finding("config", [_row("route_table", "rtb-0a7c3e5d9f1b2a4c6", ["DeleteRoute"], 2, role="cause")],
                     key_events=[cloudtrail_event])
    assert verdict(found) == "supported"
    assert verdict(_finding("config", [], anomaly=False)) == "refuted"
    assert verdict(_finding("config", [], anomaly=False, degraded=True)) == "inconclusive"
    leads = {(lead["type"], lead["id"]) for lead in find_leads(found, CAT, exclude={("route_table", "rtb-0a7c3e5d9f1b2a4c6")})}
    assert ("nat_gateway", "nat-0123456789abcdef0") in leads
    assert ("route_table", "rtb-0a7c3e5d9f1b2a4c6") not in leads


def test_absorb_makes_a_clean_domain_anomalous_once():
    clean = _finding("config", [], anomaly=False)
    scoped = _finding("config", [_row("hosted_zone", ZONE, ["ChangeResourceRecordSets"], 19, role="cause")])
    request = {"request_id": "r1", "sender": "log", "entity": {"type": "hosted_zone", "id": ZONE}}
    answer = {"finding": scoped.model_dump(mode="json")}
    revised = absorb(clean, [(request, answer)], CAT)
    assert revised.anomaly_detected and revised.revised and revised.revision_count == 1
    assert revised.evidence[0]["via"] == "validation" and revised.evidence[0]["requested_by"] == "log"
    assert "Peer validation:" in revised.summary
    assert absorb(revised, [(request, answer)], CAT) == revised  # idempotent


def test_upstream_cause_outranks_the_symptoms_it_explains():
    findings = [
        _finding("metrics", [_row("flow", "onprem-iperf-app-a", ["SuccessRate"], 2, role="symptom")]),
        _finding("routing", [_row("tgw_route_table", RTB_SPOKE, ["tgw_route_uninstalled"], 2.5, role="symptom")]),
        _finding("config", [_row("tgw_route_table", RTB_SPOKE, ["DisableTransitGatewayRouteTablePropagation"], 3,
                                 role="cause", severity="info")]),
    ]
    ranked = rank_causes(findings, TOPO, CAT, [{"type": "flow", "id": "onprem-iperf-app-a"}])
    top = ranked[0]
    assert (top.entity_type, top.entity_id, top.domain) == ("tgw_route_table", RTB_SPOKE, "config")
    assert top.features["topology"] == 1.0 and top.features["precedence"] == 1.0
    assert top.chain[0] == f"tgw_route_table:{RTB_SPOKE}" and top.chain[-1] == "flow:onprem-iperf-app-a"
    assert [c.id for c in ranked] == [f"C{i + 1}" for i in range(len(ranked))]


def test_no_ranking_without_topology():
    assert rank_causes([_finding("metrics", [_row("switch", "C", ["error_rate"], 3)])], None, CAT) == []


def test_incident_status():
    def status(*rows: dict) -> str:
        f = _finding("metrics", [])
        return incident_status([f.model_copy(update={"entity_status": list(rows)})])

    assert status({"current_state": "firing", "episodes": 1}) == "ongoing"
    assert status({"current_state": "cleared", "episodes": 1}) == "recovered"
    assert status({"current_state": "cleared", "episodes": 3}) == "flapping"
    assert status({"current_state": "unknown", "episodes": 1}) == "unknown"
    assert incident_status([]) == "unknown"
