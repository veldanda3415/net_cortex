"""Build the cross-domain replay fixtures and eval cases.

    python -m evals.build_fixtures      # writes tests/fixtures/cross_domain/*.json and evals/cases/*.json

Each case reproduces one fault from the 2026-10-28 live lab campaign
(output/alarm_bridge records), with the lab's real ids, signal shapes and
timings, plus the lab topology. Where the live run lost evidence (FRR "Bad
Peer AS" crowded out of the key events) or the fault has a property the
lab could not show in one run (a DNS record deleted longer ago than the
window, a flapping tunnel, a self-recovered BGP session), the fixture adds
it on purpose; the case's `why` says what is being tested.

Times are offsets in seconds from the alarm (negative = before). The case
runner aligns the alarm with the incident's reported_at (replay alarm_at).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "cross_domain"
CASES = ROOT / "evals" / "cases"
REGION = "us-east-1"
ALARM = datetime(2026, 10, 28, 5, 32, 0, tzinfo=timezone.utc)

# Lab ids (from the live records) ------------------------------------------------
TUN1, TUN2 = "3.86.174.132", "34.224.247.80"
VPN = "vpn-02d7b1a3f6189fd11"
NBR1, NBR2 = "169.254.100.1", "169.254.100.5"
TGW = "tgw-0033dc03a06637449"
RTB_SPOKE, RTB_ONPREM, RTB_INSPECT = "tgw-rtb-055e9770910116036", "tgw-rtb-01bdbcd48a7201538", "tgw-rtb-0282c8ef737eff348"
ATT_VPN, ATT_APP, ATT_SVC = "tgw-attach-0c6d7d687f67cd1a4", "tgw-attach-0a11e0c1a2b3c4d5e", "tgw-attach-0b22f1d2b3c4d5e6f"
VPC_APP, VPC_SVC = "vpc-0bb7177a66c8f91a9", "vpc-056cfdd14ca88fcf6"
RTB_APP = "rtb-0a7c3e5d9f1b2a4c6"
PCX = "pcx-0d4e6f8a1b3c5e7f9"
SG_APP = "sg-0d47d22a6a536ef01"
ENI_A, ENI_B = "eni-021773242b7444556", "eni-0fe0c208ffeffcb1f"
I_APP_A, I_APP_B = "i-0aa11bb22cc33dd44", "i-0ee55ff66aa77bb88"
TG_APP = "targetgroup/netcortex-lab-app/3f1c2e4d5a6b7c8d"
NLB = "net/netcortex-lab-app/75c34984f6628712"
ZONE, DNS_APP = "Z1030605VHK1NFO24118", "app.lab.internal."
FLOWS_ONPREM = ["onprem-to-nlb", "onprem-to-nlb-large", "onprem-iperf-app-a"]


def ts(offset: float) -> str:
    return (ALARM + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def ent(etype: str, eid: str, **labels: str) -> dict[str, Any]:
    return {"type": etype, "id": eid, **({"labels": labels} if labels else {})}


def topology() -> dict[str, Any]:
    entities = [
        ent("tunnel", f"{VPN}-tunnel-1", outside_ip=TUN1, capacity_bps="1250000000"),
        ent("tunnel", f"{VPN}-tunnel-2", outside_ip=TUN2, capacity_bps="1250000000"),
        ent("vpn_connection", VPN), ent("bgp_neighbor", NBR1), ent("bgp_neighbor", NBR2),
        ent("transit_gateway", TGW),
        ent("tgw_attachment", ATT_VPN, resource_type="vpn"), ent("tgw_attachment", ATT_APP, resource_type="vpc"),
        ent("tgw_attachment", ATT_SVC, resource_type="vpc"),
        ent("tgw_route_table", RTB_SPOKE), ent("tgw_route_table", RTB_ONPREM), ent("tgw_route_table", RTB_INSPECT),
        ent("vpc", VPC_APP), ent("vpc", VPC_SVC), ent("route_table", RTB_APP), ent("vpc_peering", PCX),
        ent("security_group", SG_APP), ent("eni", ENI_A), ent("eni", ENI_B),
        ent("instance", I_APP_A), ent("instance", I_APP_B),
        ent("target_group", f"arn:aws:elasticloadbalancing:{REGION}:785629662183:{TG_APP}", dimension=TG_APP),
        ent("load_balancer", f"arn:aws:elasticloadbalancing:{REGION}:785629662183:loadbalancer/{NLB}", dimension=NLB),
        ent("hosted_zone", ZONE), ent("dns_name", DNS_APP),
        *(ent("flow", f) for f in [*FLOWS_ONPREM, "svc-to-app", "app-to-svc"]),
    ]
    by = {(e["type"], e["id"]): e for e in entities}

    def e(etype: str, eid: str) -> dict[str, Any]:
        if etype == "target_group":
            eid = f"arn:aws:elasticloadbalancing:{REGION}:785629662183:{TG_APP}"
        if etype == "load_balancer":
            eid = f"arn:aws:elasticloadbalancing:{REGION}:785629662183:loadbalancer/{NLB}"
        return by[(etype, eid)]

    def rel(kind: str, src: tuple[str, str], dst: tuple[str, str]) -> dict[str, Any]:
        return {"type": kind, "source": e(*src), "target": e(*dst)}

    rels = [
        rel("member_of", ("tunnel", f"{VPN}-tunnel-1"), ("vpn_connection", VPN)),
        rel("member_of", ("tunnel", f"{VPN}-tunnel-2"), ("vpn_connection", VPN)),
        rel("runs_over", ("bgp_neighbor", NBR1), ("tunnel", f"{VPN}-tunnel-1")),
        rel("runs_over", ("bgp_neighbor", NBR2), ("tunnel", f"{VPN}-tunnel-2")),
        rel("attached_via", ("vpn_connection", VPN), ("tgw_attachment", ATT_VPN)),
        rel("attached_via", ("vpc", VPC_APP), ("tgw_attachment", ATT_APP)),
        rel("attached_via", ("vpc", VPC_SVC), ("tgw_attachment", ATT_SVC)),
        rel("associated_with", ("tgw_attachment", ATT_APP), ("tgw_route_table", RTB_SPOKE)),
        rel("associated_with", ("tgw_attachment", ATT_SVC), ("tgw_route_table", RTB_SPOKE)),
        rel("associated_with", ("tgw_attachment", ATT_VPN), ("tgw_route_table", RTB_ONPREM)),
        rel("propagates_to", ("tgw_attachment", ATT_VPN), ("tgw_route_table", RTB_SPOKE)),
        rel("propagates_to", ("tgw_attachment", ATT_APP), ("tgw_route_table", RTB_ONPREM)),
        rel("belongs_to", ("tgw_route_table", RTB_SPOKE), ("transit_gateway", TGW)),
        rel("belongs_to", ("tgw_route_table", RTB_ONPREM), ("transit_gateway", TGW)),
        rel("belongs_to", ("tgw_route_table", RTB_INSPECT), ("transit_gateway", TGW)),
        rel("in_vpc", ("route_table", RTB_APP), ("vpc", VPC_APP)),
        rel("routes_via", ("route_table", RTB_APP), ("vpc_peering", PCX)),
        rel("peers", ("vpc", VPC_APP), ("vpc_peering", PCX)),
        rel("peers", ("vpc_peering", PCX), ("vpc", VPC_SVC)),
        rel("in_vpc", ("security_group", SG_APP), ("vpc", VPC_APP)),
        rel("applies_to", ("security_group", SG_APP), ("eni", ENI_A)),
        rel("applies_to", ("security_group", SG_APP), ("eni", ENI_B)),
        rel("attached_to", ("eni", ENI_A), ("instance", I_APP_A)),
        rel("attached_to", ("eni", ENI_B), ("instance", I_APP_B)),
        rel("in_vpc", ("instance", I_APP_A), ("vpc", VPC_APP)),
        rel("in_vpc", ("instance", I_APP_B), ("vpc", VPC_APP)),
        rel("registered_in", ("instance", I_APP_A), ("target_group", "")),
        rel("registered_in", ("instance", I_APP_B), ("target_group", "")),
        rel("serves", ("target_group", ""), ("load_balancer", "")),
        rel("resolves", ("hosted_zone", ZONE), ("dns_name", DNS_APP)),
        *(rel("probes", ("flow", f), ("tunnel", f"{VPN}-tunnel-1")) for f in FLOWS_ONPREM),
        *(rel("probes", ("flow", f), ("tunnel", f"{VPN}-tunnel-2")) for f in FLOWS_ONPREM),
        rel("probes", ("flow", "onprem-to-nlb"), ("load_balancer", "")),
        rel("probes", ("flow", "onprem-to-nlb-large"), ("load_balancer", "")),
        rel("probes", ("flow", "onprem-iperf-app-a"), ("instance", I_APP_A)),
        rel("probes", ("flow", "svc-to-app"), ("dns_name", DNS_APP)),
        rel("probes", ("flow", "svc-to-app"), ("load_balancer", "")),
        rel("probes", ("flow", "app-to-svc"), ("vpc", VPC_SVC)),
    ]
    return {"entities": entities, "relationships": rels}


# Signals ------------------------------------------------------------------------

def metric(name: str, etype: str, eid: str, offset: float, value: float, *, ns: str, dim: str, stat: str = "Minimum",
           unit: str | None = None, labels: dict[str, str] | None = None) -> dict[str, Any]:
    t = ts(offset)
    return {
        "signal_type": "metric", "timestamp": t, "observed_at": ts(offset + 90),
        "entity": {"type": etype, "id": eid, "labels": labels or {dim: eid}},
        "name": name, "value": float(value), **({"unit": unit} if unit else {}), "severity": "info",
        "domain": "metrics", "source": "cloudwatch",
        "attributes": {"namespace": ns, "statistic": stat, "period": 60, "region": REGION},
        "evidence_ref": f"cloudwatch:{ns}/{name}/{dim}={eid}@{t}",
    }


def series(name: str, etype: str, eid: str, values: dict[int, float], **kw: Any) -> list[dict[str, Any]]:
    """values: minute offset -> value (one datapoint per minute)."""
    return [metric(name, etype, eid, m * 60, v, **kw) for m, v in sorted(values.items())]


def flat(start: int, end: int, value: float, **overrides: float) -> dict[int, float]:
    out = {m: value for m in range(start, end + 1)}
    out.update({int(k): v for k, v in overrides.items()})
    return out


def event(name: str, domain: str, etype: str, eid: str, offset: float, *, severity: str, source: str, ref: str,
          **attributes: Any) -> dict[str, Any]:
    return {
        "signal_type": "event", "timestamp": ts(offset), "observed_at": ts(offset + 30),
        "entity": {"type": etype, "id": eid}, "name": name, "severity": severity, "domain": domain,
        "source": source, "attributes": {**attributes, "region": REGION}, "evidence_ref": ref,
    }


def cloudtrail(name: str, etype: str, eid: str, offset: float, ref: str, request: dict[str, Any],
               read_only: bool = False) -> dict[str, Any]:
    return event(name, "config", etype, eid, offset, severity="info", source="cloudtrail", ref=f"cloudtrail:{ref}",
                 event_source="route53.amazonaws.com" if etype == "hosted_zone" else "ec2.amazonaws.com",
                 username="veldanda3415@gmail.com", read_only=read_only, request=json.dumps(request))


def background(start: int = -15, end: int = 0) -> list[dict[str, Any]]:
    """What a healthy lab minute looks like: tunnels up, BGP established,
    flows succeeding, no TGW drops, and routine read-only API calls."""
    out: list[dict[str, Any]] = []
    for tun in (TUN1, TUN2):
        out += series("TunnelState", "tunnel", tun, flat(start, end, 1), ns="AWS/VPN", dim="TunnelIpAddress")
        out += series("TunnelDataIn", "tunnel", tun, flat(start, end, 2_600_000 if tun == TUN1 else 950), ns="AWS/VPN",
                      dim="TunnelIpAddress", stat="Sum", unit="Bytes")
    for nbr in (NBR1, NBR2):
        out += series("BgpPeerState", "bgp_neighbor", nbr, flat(start, end, 1), ns="NetCortexLab/Router", dim="Neighbor")
        out += series("PrefixesSent", "bgp_neighbor", nbr, flat(start, end, 3), ns="NetCortexLab/Router", dim="Neighbor")
    for flow in [*FLOWS_ONPREM, "svc-to-app", "app-to-svc"]:
        out += series("SuccessRate", "flow", flow, flat(start, end, 100), ns="NetCortexLab/Traffic", dim="Flow", unit="Percent")
    out += series("PacketDropCountNoRoute", "transit_gateway", TGW, flat(start, end, 0), ns="AWS/TransitGateway",
                  dim="TransitGateway", stat="Sum", unit="Count")
    out += [
        cloudtrail("DescribeRouteTables", "route_table", RTB_APP, -600, "bg-0001", {}, read_only=True),
        cloudtrail("DescribeTransitGatewayAttachments", "transit_gateway", TGW, -300, "bg-0002", {}, read_only=True),
    ]
    return out


def override(signals: list[dict[str, Any]], name: str, eid: str, values: dict[int, float]) -> list[dict[str, Any]]:
    out = []
    for s in signals:
        if s["name"] == name and s["entity"]["id"] == eid:
            minute = round((datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - ALARM).total_seconds() / 60)
            if minute in values:
                s = {**s, "value": float(values[minute])}
        out.append(s)
    return out


# Cases --------------------------------------------------------------------------

def case_tgw_propagation_removed() -> tuple[list, dict]:
    s = background()
    s = override(s, "SuccessRate", "onprem-iperf-app-a", {-2: 0, -1: 0, 0: 0})
    s = override(s, "SuccessRate", "onprem-to-nlb", {-2: 76.9, -1: 0, 0: 0})
    s = override(s, "PacketDropCountNoRoute", TGW, {-2: 412, -1: 1830, 0: 1795})
    s += [
        cloudtrail("DisableTransitGatewayRouteTablePropagation", "tgw_route_table", RTB_SPOKE, -170, "0d27b5d1-f4f6-4643-aa7b-017d920cdf1f",
                   {"DisableTransitGatewayRouteTablePropagationRequest": {"TransitGatewayAttachmentId": ATT_VPN, "TransitGatewayRouteTableId": RTB_SPOKE}}),
        event("tgw_route_uninstalled", "routing", "tgw_route_table", RTB_SPOKE, -145, severity="warning", source="cloudwatch_logs",
              ref="cloudwatch_logs:/netcortex-lab/networkmanager/events/a74c5fd7", log_group="/netcortex-lab/networkmanager/events",
              message="Network Manager Routing Update TGW-ROUTE-UNINSTALLED 172.16.0.0/16 in " + RTB_SPOKE),
    ]
    return s, {
        "fault": "tgw-propagation-removed",
        "description": "CloudWatch alarms went into ALARM: netcortex-lab-flow-onprem-iperf-app-a-degraded; netcortex-lab-flow-onprem-to-nlb-degraded",
        "alarm_entities": [{"type": "flow", "id": "onprem-iperf-app-a"}, {"type": "flow", "id": "onprem-to-nlb"}],
        "culprit": {"type": "tgw_route_table", "ids": [RTB_SPOKE], "domain": "config"},
        "why": "A route-table change (config) explains flow loss (metrics) four topology hops away.",
    }


def case_asn_mismatch() -> tuple[list, dict]:
    s = background()
    s = override(s, "TunnelState", TUN1, {-3: 0, -2: 0, -1: 0, 0: 0})
    s = override(s, "BgpPeerState", NBR1, {-3: 0, -2: 0, -1: 0, 0: 0})
    s = override(s, "PrefixesSent", NBR1, {-3: 0, -2: 0, -1: 0, 0: 0})
    s += [event("bgp_open_error", "logs", "bgp_neighbor", NBR1, -185, severity="error", source="cloudwatch_logs",
                ref="cloudwatch_logs:/netcortex-lab/router/frr/i-08306fe7b969eb0ef/0001", log_group="/netcortex-lab/router/frr",
                message=f"%NOTIFICATION: sent to neighbor {NBR1} 2/2 (OPEN Message Error/Bad Peer AS) 2 bytes 7d 7f")]
    s += [event("bgp_neighbor_down", "logs", "bgp_neighbor", NBR1, -184, severity="error", source="cloudwatch_logs",
                ref="cloudwatch_logs:/netcortex-lab/router/frr/i-08306fe7b969eb0ef/0002", log_group="/netcortex-lab/router/frr",
                message=f"%ADJCHANGE: neighbor {NBR1}(Unknown) in vrf default Down BGP Notification send")]
    # The AWS side retries every ~10 s and logs the same lines each time: the
    # burst that crowded "Bad Peer AS" out of the live run's key events.
    for i in range(16):
        s.append(event("vpn_bgp_status_down", "logs", "tunnel", TUN1, -180 + i * 10, severity="error", source="cloudwatch_logs",
                       ref=f"cloudwatch_logs:/netcortex-lab/vpn/bgp/{VPN}-{TUN1}-BGP.log/{1000 + i}", log_group="/netcortex-lab/vpn/bgp",
                       details=f"AWS-side peer BGP session state has changed from Established to Clearing with neighbor 169.254.100.2 ({i})",
                       status="DOWN"))
    s.append(event("vpn_bgp_down", "routing", "tunnel", TUN1, -174, severity="error", source="cloudwatch_logs",
                   ref="cloudwatch_logs:/netcortex-lab/networkmanager/events/4b288b7d", log_group="/netcortex-lab/networkmanager/events",
                   message="Network Manager Status Update VPN-CONNECTION-BGP-DOWN"))
    return s, {
        "fault": "asn-mismatch",
        "description": f"CloudWatch alarms went into ALARM: netcortex-lab-bgp-{NBR1}-down; netcortex-lab-vpn-tunnel1-down",
        "alarm_entities": [{"type": "bgp_neighbor", "id": NBR1}, {"type": "tunnel", "id": TUN1}],
        "culprit": {"type": "bgp_neighbor", "ids": [NBR1], "domain": "log"},
        "why": "The decisive FRR 'Bad Peer AS' line (log) is buried under 16 repeated AWS BGP DOWN lines; the alarm is a metric.",
    }


def case_sg_rule_removed() -> tuple[list, dict]:
    s = background()
    s = override(s, "SuccessRate", "onprem-to-nlb", {-2: 16.7, -1: 0, 0: 0})
    s = override(s, "SuccessRate", "onprem-to-nlb-large", {-2: 0, -1: 0, 0: 0})
    s += [cloudtrail("ModifySecurityGroupRules", "security_group", SG_APP, -200, "bb9d76e9-188e-422f-ad04-c8dae3739873",
                     {"ModifySecurityGroupRulesRequest": {"GroupId": SG_APP, "SecurityGroupRule": {"CidrIpv4": "192.0.2.0/24", "FromPort": 80, "ToPort": 80}}})]
    for minute, eni, n in [(-3, ENI_A, 1), (-2, ENI_B, 9), (-2, ENI_A, 10), (-1, ENI_B, 10), (-1, ENI_A, 9), (0, ENI_A, 11)]:
        sig = metric("flow_rejects", "eni", eni, minute * 60, n, ns="", dim="")
        sig.update({"domain": "logs", "source": "cloudwatch_logs", "unit": "Count", "severity": "warning",
                    "entity": {"type": "eni", "id": eni},
                    "attributes": {"log_group": "/netcortex-lab/vpc-flow/app", "vpc": VPC_APP, "region": REGION,
                                   "sample": f"5 785629662183 {eni} 172.16.10.52 10.10.22.108 48448 80 6 4 240 REJECT OK"},
                    "evidence_ref": f"cloudwatch_logs:/netcortex-lab/vpc-flow/app/flow_rejects/{eni}@{ts(minute * 60)}"})
        s.append(sig)
    return s, {
        "fault": "sg-rule-removed",
        "description": "CloudWatch alarms went into ALARM: netcortex-lab-flow-onprem-to-nlb-large-degraded; netcortex-lab-flow-onprem-to-nlb-degraded",
        "alarm_entities": [{"type": "flow", "id": "onprem-to-nlb"}, {"type": "flow", "id": "onprem-to-nlb-large"}],
        "culprit": {"type": "security_group", "ids": [SG_APP], "domain": "config"},
        "why": "A security-group change (config) behind the NLB explains flow loss (metrics) and REJECTs on the ENIs (log).",
    }


def case_phz_record_deleted() -> tuple[list, dict]:
    s = background()
    s = override(s, "SuccessRate", "svc-to-app", {-3: 91.7, -2: 0, -1: 0, 0: 0})
    for minute in (-4, -3, -2, -1, 0):
        sig = metric("dns_nxdomain", "dns_name", DNS_APP, minute * 60, 1 if minute < -2 else 6, ns="", dim="")
        sig.update({"domain": "logs", "source": "cloudwatch_logs", "unit": "Count", "severity": "warning",
                    "entity": {"type": "dns_name", "id": DNS_APP},
                    "attributes": {"log_group": "/netcortex-lab/dns/resolver", "region": REGION,
                                   "sample": json.dumps({"vpc_id": VPC_SVC, "query_name": DNS_APP, "rcode": "NXDOMAIN"})},
                    "evidence_ref": f"cloudwatch_logs:/netcortex-lab/dns/resolver/dns_nxdomain/{DNS_APP}@{ts(minute * 60)}"})
        s.append(sig)
    # The record was deleted 19 minutes before the alarm: resolvers served the
    # cached answer until its 900 s TTL ran out. That is before a 15-minute
    # window, so round 0 sees no config change at all.
    s.append(cloudtrail("ChangeResourceRecordSets", "hosted_zone", ZONE, -19 * 60, "5e1f0a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b",
                        {"hostedZoneId": ZONE, "changeBatch": {"changes": [{"action": "DELETE", "resourceRecordSet": {"name": DNS_APP, "type": "A"}}]}}))
    return s, {
        "fault": "phz-record-deleted",
        "description": "CloudWatch alarms went into ALARM: netcortex-lab-flow-svc-to-app-degraded",
        "alarm_entities": [{"type": "flow", "id": "svc-to-app"}],
        "culprit": {"type": "hosted_zone", "ids": [ZONE], "domain": "config"},
        "why": "The cause predates the analysis window (DNS TTL); only a peer's targeted question with a lookback finds it.",
    }


def case_peering_route_removed() -> tuple[list, dict]:
    s = background()
    s = override(s, "SuccessRate", "app-to-svc", {-2: 40, -1: 0, 0: 0})
    s.append(cloudtrail("DeleteRoute", "route_table", RTB_APP, -150, "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d",
                        {"routeTableId": RTB_APP, "destinationCidrBlock": "10.30.0.0/16"}))
    return s, {
        "fault": "peering-route-removed",
        "description": "CloudWatch alarms went into ALARM: netcortex-lab-flow-app-to-svc-degraded",
        "alarm_entities": [{"type": "flow", "id": "app-to-svc"}],
        "culprit": {"type": "route_table", "ids": [RTB_APP], "domain": "config"},
        "why": "A deleted peering route (config) explains a failing flow (metrics) across the VPC peering.",
    }


def case_bad_route_map() -> tuple[list, dict]:
    s = background()
    s = override(s, "PrefixesSent", NBR1, {-3: 0, -2: 0, -1: 0, 0: 0})
    s = override(s, "PrefixesSent", NBR2, {-3: 0, -2: 0, -1: 0, 0: 0})
    s = override(s, "SuccessRate", "onprem-to-nlb", {-1: 88.2, 0: 0})
    s = override(s, "SuccessRate", "onprem-iperf-app-a", {-1: 0, 0: 0})
    s = override(s, "PacketDropCountNoRoute", TGW, {-1: 920, 0: 1404})
    s += [event("tgw_route_uninstalled", "routing", "tgw_route_table", rtb, off, severity="warning", source="cloudwatch_logs",
                ref=f"cloudwatch_logs:/netcortex-lab/networkmanager/events/{rtb}", log_group="/netcortex-lab/networkmanager/events",
                message=f"Network Manager Routing Update TGW-ROUTE-UNINSTALLED 172.16.0.0/16 in {rtb}")
          for rtb, off in ((RTB_SPOKE, -171), (RTB_INSPECT, -165))]
    return s, {
        "fault": "bad-route-map",
        "description": "CloudWatch alarms went into ALARM: netcortex-lab-flow-onprem-to-nlb-degraded; netcortex-lab-flow-onprem-iperf-app-a-degraded",
        "alarm_entities": [{"type": "flow", "id": "onprem-to-nlb"}, {"type": "flow", "id": "onprem-iperf-app-a"}],
        "culprit": {"type": "bgp_neighbor", "ids": [NBR1, NBR2], "domain": "metrics"},
        "why": "The router stopped advertising prefixes (bgp_neighbor) while BGP stayed up; routes vanished from TGW (routing) and flows failed.",
    }


def case_loss_latency() -> tuple[list, dict]:
    s = background(start=-15, end=0)
    for flow in FLOWS_ONPREM:
        s += series("LatencyMs", "flow", flow, flat(-15, 0, 4.0, **{str(m): 156.0 for m in range(-5, 1)}),
                    ns="NetCortexLab/Traffic", dim="Flow", stat="Average", unit="Milliseconds")
        s += series("LossPercent", "flow", flow, flat(-15, 0, 0.0, **{str(m): 5.2 for m in range(-5, 1)}),
                    ns="NetCortexLab/Traffic", dim="Flow", stat="Average", unit="Percent")
    return s, {
        "fault": "loss-latency",
        "description": "CloudWatch alarms went into ALARM: netcortex-lab-flow-onprem-to-nlb-latency; netcortex-lab-flow-onprem-to-nlb-loss",
        "alarm_entities": [{"type": "flow", "id": "onprem-to-nlb"}],
        "culprit": None,
        "why": "Loss and latency with no control-plane change: the new LatencyMs/LossPercent rules see the symptom, and nothing is blamed on config.",
    }


def case_tunnel_flapping() -> tuple[list, dict]:
    s = background(start=-15, end=10)
    flap = {m: (0 if m % 2 == 0 else 1) for m in range(-6, 11)}
    s = override(s, "TunnelState", TUN2, flap)
    return s, {
        "fault": "tunnel-down",
        "description": "CloudWatch alarms went into ALARM: netcortex-lab-vpn-tunnel2-down",
        "alarm_entities": [{"type": "tunnel", "id": TUN2}],
        "culprit": {"type": "tunnel", "ids": [TUN2, f"{VPN}-tunnel-2"], "domain": "metrics"},
        "minutes_since_alarm": 10,
        "expect_status": "flapping",
        "why": "The tunnel keeps going down and up after the alarm: report flapping, not a steady outage.",
    }


def case_bgp_self_recovered() -> tuple[list, dict]:
    s = background(start=-15, end=10)
    s = override(s, "BgpPeerState", NBR2, {-2: 0, -1: 0, 0: 0})
    s.append(event("bgp_hold_timer_expired", "logs", "bgp_neighbor", NBR2, -125, severity="error", source="cloudwatch_logs",
                   ref="cloudwatch_logs:/netcortex-lab/router/frr/i-08306fe7b969eb0ef/0100", log_group="/netcortex-lab/router/frr",
                   message=f"%NOTIFICATION: sent to neighbor {NBR2} 4/0 (Hold Timer Expired) 0 bytes"))
    return s, {
        "fault": "bgp-neighbor-shutdown",
        "description": f"CloudWatch alarms went into ALARM: netcortex-lab-bgp-{NBR2}-down",
        "alarm_entities": [{"type": "bgp_neighbor", "id": NBR2}],
        "culprit": {"type": "bgp_neighbor", "ids": [NBR2], "domain": "metrics"},
        "minutes_since_alarm": 10,
        "expect_status": "recovered",
        "why": "The BGP session dropped and came back on its own within a minute of the alarm: recovered, not an outage.",
    }


def case_healthy() -> tuple[list, dict]:
    return background(), {
        "fault": "mss-clamp-removed",
        "description": "Scheduled health check: no alarm",
        "alarm_entities": [],
        "culprit": None,
        "why": "Negative control: a healthy lab must produce a clean report.",
    }


BUILDERS = [
    (2001, case_tgw_propagation_removed), (2002, case_asn_mismatch), (2003, case_sg_rule_removed),
    (2004, case_phz_record_deleted), (2005, case_peering_route_removed), (2006, case_bad_route_map),
    (2007, case_loss_latency), (2008, case_tunnel_flapping), (2009, case_bgp_self_recovered), (2010, case_healthy),
]


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    CASES.mkdir(parents=True, exist_ok=True)
    topo = topology()
    for scenario_id, build in BUILDERS:
        signals, case = build()
        name = build.__name__.removeprefix("case_").replace("_", "-")
        times = [s["timestamp"] for s in signals]
        fixture = {
            "contract_version": "2.1",
            "description": f"{case['fault']}: {case['why']}",
            "region": REGION,
            "scenario_id": scenario_id,
            "recorded_window": {"start": min(times), "end": max(max(times), ts(0))},
            "alarm_at": ts(0),
            "signals": sorted(signals, key=lambda s: (s["timestamp"], s["name"], s["entity"]["id"])),
            "topology": topo,
        }
        (FIXTURES / f"{name}.json").write_text(json.dumps(fixture, indent=1) + "\n", encoding="utf-8")
        (CASES / f"{name}.json").write_text(json.dumps({
            "name": name, "scenario_id": scenario_id, "fixture": f"tests/fixtures/cross_domain/{name}.json",
            "region": REGION, "severity": "high", **case,
        }, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {name}: {len(signals)} signals")


if __name__ == "__main__":
    main()
