"""AWS source plugins against stubbed AWS API responses (botocore Stubber, no
network or credentials), then through the default catalog's detection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

from contract import TelemetryQuery, ToolResponse
from contract.catalog_loader import load_catalog
from detection import analyze_domain
from mcp_telemetry_server.plugins.aws.cloudtrail import CloudTrailPlugin
from mcp_telemetry_server.plugins.aws.cloudwatch import CloudWatchPlugin
from mcp_telemetry_server.plugins.aws.logs import CloudWatchLogsPlugin
from mcp_telemetry_server.plugins.aws.tgw import TgwRoutesPlugin
from mcp_telemetry_server.service import TelemetryService, load_sources_config

REGION = "us-east-1"
END = datetime.now(timezone.utc).replace(second=0, microsecond=0)
QUERY = TelemetryQuery(region=REGION, start_time=END - timedelta(minutes=30), end_time=END)
CATALOG = load_catalog()


def _stub(plugin, service: str) -> Stubber:
    client = boto3.client(service, region_name=REGION, aws_access_key_id="x", aws_secret_access_key="x")
    plugin._clients[(service, REGION)] = client
    stubber = Stubber(client)
    stubber.activate()
    return stubber


def _finding(domain_id: str, signals, coverage, description="On-prem users cannot reach the app"):
    response = ToolResponse(window=QUERY.window, coverage=[coverage], signals=signals)
    return analyze_domain(CATALOG, domain_id, response, region=REGION, incident_description=description,
                          baseline_lookup=lambda k, m: None)


def _fired(finding) -> set[str]:
    return {r["signal"] for e in finding.key_events for r in e.get("rules_fired", [])}


# ---------------------------------------------------------------------------
# cloudwatch
# ---------------------------------------------------------------------------

CW_CONFIG = {"metrics": [
    {"namespace": "AWS/VPN", "names": ["TunnelState"], "stat": "Minimum", "dimensions": ["TunnelIpAddress"],
     "entity": {"type": "tunnel", "id": "TunnelIpAddress"}},
]}


@pytest.mark.asyncio
async def test_cloudwatch_reads_exact_dimension_series_and_detects_tunnel_down():
    plugin = CloudWatchPlugin(CW_CONFIG)
    stub = _stub(plugin, "cloudwatch")
    stub.add_response("list_metrics", {"Metrics": [
        {"Namespace": "AWS/VPN", "MetricName": "TunnelState", "Dimensions": [{"Name": "TunnelIpAddress", "Value": "3.3.3.1"}]},
        {"Namespace": "AWS/VPN", "MetricName": "TunnelState", "Dimensions": [{"Name": "TunnelIpAddress", "Value": "3.3.3.2"}]},
        # Same metric with an extra dimension: a different series, skipped.
        {"Namespace": "AWS/VPN", "MetricName": "TunnelState",
         "Dimensions": [{"Name": "VpnId", "Value": "vpn-1"}, {"Name": "TunnelIpAddress", "Value": "3.3.3.1"}]},
    ]}, {"Namespace": "AWS/VPN", "MetricName": "TunnelState", "Dimensions": [{"Name": "TunnelIpAddress"}]})
    t1, t2 = END - timedelta(minutes=5), END - timedelta(minutes=4)
    stub.add_response("get_metric_data", {"MetricDataResults": [
        {"Id": "m0", "Timestamps": [t1, t2], "Values": [1.0, 0.0], "StatusCode": "Complete"},
        {"Id": "m1", "Timestamps": [t1, t2], "Values": [1.0, 1.0], "StatusCode": "Complete"},
    ]}, {"MetricDataQueries": ANY, "StartTime": QUERY.start_time, "EndTime": END, "ScanBy": "TimestampAscending"})

    signals, coverage = await plugin.fetch(QUERY, domain="metrics")
    stub.assert_no_pending_responses()

    assert coverage.status == "ok" and len(signals) == 4
    assert {s.entity.id for s in signals} == {"3.3.3.1", "3.3.3.2"}
    assert all(s.unit == "None" and s.entity.type == "tunnel" for s in signals)  # unit from the catalog
    down = [s for s in signals if s.value == 0.0]
    assert down[0].evidence_ref == f"cloudwatch:AWS/VPN/TunnelState/TunnelIpAddress=3.3.3.1@{t2.isoformat()}"

    finding = _finding("metrics", signals, coverage)
    assert finding.anomaly_detected and _fired(finding) == {"TunnelState"}
    assert {e["entity_id"] for e in finding.key_events} == {"3.3.3.1"}


def test_cloudwatch_entity_id_must_be_a_dimension():
    bad = {"metrics": [{**CW_CONFIG["metrics"][0], "entity": {"type": "tunnel", "id": "VpnId"}}]}
    with pytest.raises(ValueError, match="not one of its dimensions"):
        CloudWatchPlugin(bad)


# ---------------------------------------------------------------------------
# cloudwatch_logs
# ---------------------------------------------------------------------------

def _event(minutes_ago: float, message: str, stream="i-0router", eid="e") -> dict:
    ts = int((END - timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return {"logStreamName": stream, "timestamp": ts, "message": message, "eventId": f"{eid}{ts}"}


FRR_QUERY = {
    "id": "frr", "log_group": "/lab/router/frr", "filter_pattern": "?ADJCHANGE ?NOTIFICATION",
    "entity": {"type": "bgp_neighbor"},
    "classify": [
        {"name": "bgp_open_error", "pattern": r"NOTIFICATION: (?:sent to|received from) neighbor (?P<id>[0-9.]+) 2/"},
        {"name": "bgp_neighbor_down", "pattern": r"ADJCHANGE: neighbor (?P<id>[0-9.]+)\S* .*\bDown\b"},
    ],
}


@pytest.mark.asyncio
async def test_logs_names_errors_at_the_source_and_drops_unmatched_lines():
    plugin = CloudWatchLogsPlugin({"queries": [FRR_QUERY]})
    stub = _stub(plugin, "logs")
    stub.add_response("filter_log_events", {"events": [
        _event(9, "bgpd[812]: [M59KS-A3ZXZ] %ADJCHANGE: neighbor 169.254.10.1(Unknown) in vrf default Up"),
        _event(6, "bgpd[812]: %NOTIFICATION: sent to neighbor 169.254.10.1 2/2 (OPEN Message Error/Bad Peer AS) 2 bytes fd e7"),
        _event(5, "bgpd[812]: %ADJCHANGE: neighbor 169.254.10.1(Unknown) in vrf default Down Peer closed the session"),
    ]}, {"logGroupName": "/lab/router/frr", "startTime": ANY, "endTime": ANY, "filterPattern": "?ADJCHANGE ?NOTIFICATION"})

    signals, coverage = await plugin.fetch(QUERY, domain="logs")

    assert coverage.status == "ok"
    assert [s.name for s in signals] == ["bgp_open_error", "bgp_neighbor_down"]   # the Up line matched nothing
    assert all(s.entity.type == "bgp_neighbor" and s.entity.id == "169.254.10.1" for s in signals)
    assert signals[0].evidence_ref.startswith("cloudwatch_logs:/lab/router/frr/i-0router/e")
    finding = _finding("log", signals, coverage)
    assert finding.anomaly_detected and _fired(finding) == {"bgp_open_error", "bgp_neighbor_down"}


@pytest.mark.asyncio
async def test_logs_counts_per_minute_from_space_delimited_flow_logs():
    fields = ["version", "account-id", "interface-id", "srcaddr", "dstaddr", "srcport", "dstport", "protocol",
              "packets", "bytes", "start", "end", "action", "log-status"]
    plugin = CloudWatchLogsPlugin({"queries": [{
        "id": "flow", "log_group": "/lab/vpc-flow/app", "format": "fields", "fields": fields, "filter_pattern": "REJECT",
        "entity": {"type": "eni", "field": "interface-id"},
        "classify": [{"name": "flow_rejects", "field": "action", "equals": "REJECT", "severity": "warning"}],
        "aggregate": "count_per_minute", "attributes": {"dst": "dstaddr"},
    }]})
    stub = _stub(plugin, "logs")
    line = "2 111 eni-0aaa 172.16.1.10 10.10.1.5 40000 80 6 3 180 1 2 {} OK"
    events = [_event(4.5, line.format("REJECT"), eid=str(i)) for i in range(12)] + [_event(3.5, line.format("REJECT"), eid="x")]
    stub.add_response("filter_log_events", {"events": events}, None)

    signals, _ = await plugin.fetch(QUERY, domain="logs")

    assert [(s.name, s.value, s.signal_type) for s in signals] == [("flow_rejects", 12.0, "metric"), ("flow_rejects", 1.0, "metric")]
    assert signals[0].attributes["dst"] == "10.10.1.5" and "sample" in signals[0].attributes
    assert "message" not in signals[0].attributes
    finding = _finding("log", signals, _ok("cloudwatch_logs"))
    assert finding.anomaly_detected and len([e for e in finding.key_events if e["rules_fired"]]) == 1  # only the 12/min minute


def _ok(source: str):
    from contract import Coverage
    return Coverage(source=source, status="ok")


@pytest.mark.asyncio
async def test_logs_parses_network_manager_events_into_routing_signals():
    plugin = CloudWatchLogsPlugin({"queries": [{
        "id": "nm", "log_group": "/lab/nm", "domain": "routing", "format": "json", "timestamp_field": "time",
        "entity": {"type": "tgw_route_table", "field": "detail.transitGatewayRouteTableArns.0",
                   "pattern": "(?P<id>tgw-rtb-[0-9a-f]+)"},
        "classify": [
            {"name": "tgw_route_uninstalled", "field": "detail.changeType", "equals": "TGW-ROUTE-UNINSTALLED", "severity": "warning"},
            {"name": "vpn_ipsec_down", "field": "detail.changeType", "equals": "VPN-CONNECTION-IPSEC-DOWN",
             "entity": {"type": "tunnel", "field": "detail.outsideIpAddress"}},
        ],
    }]})
    stub = _stub(plugin, "logs")
    when = (END - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    uninstalled = {"detail-type": "Network Manager Routing Update", "time": when, "detail": {
        "changeType": "TGW-ROUTE-UNINSTALLED",
        "transitGatewayRouteTableArns": ["arn:aws:ec2:us-east-1:1:transit-gateway-route-table/tgw-rtb-0abc"],
        "routes": [{"destinationCidrBlock": "172.16.0.0/16", "routeState": "active"}]}}
    ipsec = {"time": when, "detail": {"changeType": "VPN-CONNECTION-IPSEC-DOWN", "outsideIpAddress": "3.3.3.1"}}
    installed = {"time": when, "detail": {"changeType": "TGW-ROUTE-INSTALLED"}}
    stub.add_response("filter_log_events", {"events": [
        _event(2, json.dumps(e), stream="nm", eid=str(i)) for i, e in enumerate([uninstalled, ipsec, installed])
    ]}, None)

    signals, _ = await plugin.fetch(QUERY, domain="routing")

    assert [(s.name, s.entity.type, s.entity.id) for s in signals] == [
        ("tgw_route_uninstalled", "tgw_route_table", "tgw-rtb-0abc"), ("vpn_ipsec_down", "tunnel", "3.3.3.1")]
    assert all(s.timestamp == END - timedelta(minutes=3) for s in signals)  # event time, not ingest time
    assert await plugin.fetch(QUERY, domain="logs") == ([], _ok_cov(plugin))


def _ok_cov(plugin):
    return plugin.coverage("ok", truncated=False, error=None)


@pytest.mark.asyncio
async def test_logs_one_unreadable_group_is_partial_and_optional_missing_group_is_skipped(monkeypatch):
    monkeypatch.setattr("mcp_telemetry_server.plugins.aws.logs.MAX_CONCURRENT_QUERIES", 1)  # stub responses in query order
    plugin = CloudWatchLogsPlugin({"queries": [
        {**FRR_QUERY, "id": "a"},
        {**FRR_QUERY, "id": "b", "log_group": "/lab/missing"},
        {**FRR_QUERY, "id": "c", "log_group": "/lab/firewall", "optional": True},
    ]})
    stub = _stub(plugin, "logs")
    stub.add_response("filter_log_events", {"events": []}, None)
    stub.add_client_error("filter_log_events", "ResourceNotFoundException", "missing")
    stub.add_client_error("filter_log_events", "ResourceNotFoundException", "missing")

    _, coverage = await plugin.fetch(QUERY, domain="logs")

    assert coverage.status == "partial" and coverage.error.startswith("b: ")   # c is optional: not reported


def test_logs_refuses_a_denied_log_group():
    with pytest.raises(ValueError, match="deny_log_groups"):
        CloudWatchLogsPlugin({"deny_log_groups": ["/netcortex-lab/faults"],
                              "queries": [{**FRR_QUERY, "log_group": "/netcortex-lab/faults"}]})
    with pytest.raises(ValueError, match="deny_log_groups"):
        CloudWatchLogsPlugin({"deny_log_groups": ["/netcortex-lab/fault*"],
                              "queries": [{**FRR_QUERY, "log_group": "/netcortex-lab/faults"}]})


# ---------------------------------------------------------------------------
# cloudtrail
# ---------------------------------------------------------------------------

def _trail(name: str, source: str, request: dict, minutes_ago: float, error: str | None = None) -> dict:
    body = {"eventSource": source, "requestParameters": request, **({"errorCode": error} if error else {})}
    return {"EventId": f"id-{name}", "EventName": name, "EventSource": source, "ReadOnly": "false",
            "EventTime": END - timedelta(minutes=minutes_ago), "Username": "admin",
            "Resources": [], "CloudTrailEvent": json.dumps(body)}


@pytest.mark.asyncio
async def test_cloudtrail_keeps_allowed_sources_and_finds_the_changed_resource():
    plugin = CloudTrailPlugin({})
    stub = _stub(plugin, "cloudtrail")
    stub.add_response("lookup_events", {"Events": [
        _trail("ModifySecurityGroupRules", "ec2.amazonaws.com",
               {"ModifySecurityGroupRulesRequest": {"GroupId": "sg-0web", "SecurityGroupRule": {"SecurityGroupRuleId": "sgr-1"}}}, 6),
        _trail("SendCommand", "ssm.amazonaws.com", {"comment": "netcortex fault sg-rule-removed/inject"}, 7),
        _trail("CreateLogStream", "logs.amazonaws.com", {"logGroupName": "/netcortex-lab/faults"}, 7),
        _trail("CreateRoute", "ec2.amazonaws.com", {"routeTableId": "rtb-1"}, 8, error="Client.UnauthorizedOperation"),
        _trail("ChangeResourceRecordSets", "route53.amazonaws.com", {"hostedZoneId": "/hostedzone/Z123"}, 5),
    ]}, {"StartTime": QUERY.start_time, "EndTime": END,
         "LookupAttributes": [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}]})

    signals, coverage = await plugin.fetch(QUERY, domain="config")

    assert coverage.status == "ok"
    names = [(s.name, s.entity.type, s.entity.id, s.severity) for s in signals]
    assert names == [
        ("CreateRoute", "route_table", "rtb-1", "debug"),           # failed call: kept, but changed nothing
        ("ModifySecurityGroupRules", "security_group", "sg-0web", "info"),
        ("ChangeResourceRecordSets", "hosted_zone", "Z123", "info"),
    ]
    assert not any("fault" in json.dumps(s.attributes) for s in signals)   # ssm/logs events never reach NetCortex
    finding = _finding("config", signals, coverage)
    assert finding.anomaly_detected
    assert _fired(finding) == {"ModifySecurityGroupRules", "ChangeResourceRecordSets"}


# ---------------------------------------------------------------------------
# tgw_routes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tgw_routes_emits_blackholes_and_skips_old_windows():
    plugin = TgwRoutesPlugin({})
    stub = _stub(plugin, "ec2")
    stub.add_response("describe_transit_gateway_route_tables", {"TransitGatewayRouteTables": [
        {"TransitGatewayRouteTableId": "tgw-rtb-1", "TransitGatewayId": "tgw-1", "State": "available"}]}, {})
    stub.add_response("search_transit_gateway_routes", {"Routes": [
        {"DestinationCidrBlock": "10.10.1.0/24", "Type": "static", "State": "blackhole"}],
        "AdditionalRoutesAvailable": False},
        {"TransitGatewayRouteTableId": "tgw-rtb-1", "Filters": [{"Name": "state", "Values": ["blackhole"]}], "MaxResults": 200})

    now_query = TelemetryQuery(region=REGION, start_time=datetime.now(timezone.utc) - timedelta(minutes=30),
                               end_time=datetime.now(timezone.utc))
    signals, coverage = await plugin.fetch(now_query, domain="routing")
    assert coverage.status == "ok" and [s.attributes["state"] for s in signals] == ["blackhole"]
    assert signals[0].timestamp <= now_query.end_time and signals[0].attributes["snapshot"] is True

    old = TelemetryQuery(region=REGION, start_time=END - timedelta(days=1, minutes=30), end_time=END - timedelta(days=1))
    signals, coverage = await plugin.fetch(old, domain="routing")
    assert signals == [] and coverage.status == "partial" and "snapshot skipped" in coverage.error


def test_aws_plugins_reject_non_aws_regions():
    with pytest.raises(ValueError, match="not an AWS region"):
        TgwRoutesPlugin({"region": "us-east"})
    with pytest.raises(ValueError, match="not an AWS region"):
        TgwRoutesPlugin({}).region_for(TelemetryQuery(region="us-east", start_time=END, end_time=END))


def test_aws_lab_sources_config_loads():
    service = TelemetryService(load_sources_config("config/telemetry.aws-lab.yaml"))
    assert {p.name for p in service.plugins} == {"cloudwatch", "cloudwatch_logs", "cloudtrail", "tgw_routes"}
