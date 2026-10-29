"""Prometheus/Mimir metrics and AWS topology source plugins (no network)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from contract import TelemetryQuery, ToolResponse, TopologyResponse
from contract.catalog_loader import get_catalog
from detection import analyze_domain
from detection.investigate import TopologyGraph
from mcp_telemetry_server.plugins.aws.topology import AwsTopologyPlugin
from mcp_telemetry_server.plugins.prometheus import PrometheusPlugin

END = datetime(2026, 10, 28, 5, 30, tzinfo=timezone.utc)
QUERY = TelemetryQuery(region="dc-east-1", start_time=END - timedelta(minutes=10), end_time=END)


def _prom(handler) -> PrometheusPlugin:
    cfg = {
        "url": "https://mimir.example/prometheus", "tenant": "netops", "auth": "bearer_env", "token_env": "PROM_TOKEN",
        "queries": [{"name": "link_out_bps", "expr": "rate(ifHCOutOctets[5m]) * 8",
                     "entity": {"type": "interface", "id": "ifName", "labels": ["instance"]},
                     "capacity_expr": "ifHighSpeed * 1e6", "unit": "Bits/Second"}],
    }
    return PrometheusPlugin(cfg, transport=httpx.MockTransport(handler))


def test_prometheus_series_become_signals_with_capacity(monkeypatch):
    monkeypatch.setenv("PROM_TOKEN", "t0k")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        query = request.url.params["query"]
        if query.startswith("ifHighSpeed"):
            result = [{"metric": {"ifName": "xe-0/0/7"}, "values": [[END.timestamp(), "10000000000"]]}]
        else:
            result = [{"metric": {"ifName": "xe-0/0/7", "instance": "edge-22"},
                       "values": [[(END - timedelta(minutes=1)).timestamp(), "9.1e9"], [END.timestamp(), "NaN-ish"]]}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": result}})

    signals, coverage = asyncio.run(_prom(handler).fetch(QUERY, domain="metrics"))
    assert coverage.status == "ok"
    assert all(r.headers["X-Scope-OrgID"] == "netops" and r.headers["Authorization"] == "Bearer t0k" for r in seen)
    assert len(signals) == 1  # the unparseable sample is skipped
    s = signals[0]
    assert (s.entity.type, s.entity.id, s.entity.labels) == ("interface", "xe-0/0/7", {"instance": "edge-22", "capacity_bps": "10000000000"})
    assert s.evidence_ref.startswith("prometheus:link_out_bps{")
    # 9.1 Gb/s on a 10 Gb/s link is above the catalog's 80% utilization rule.
    finding = analyze_domain(get_catalog(), "metrics", ToolResponse(window=QUERY.window, signals=signals), region="dc-east-1")
    assert finding.anomaly_detected and finding.evidence[0]["signals"] == ["link_out_bps"]


def test_prometheus_error_is_raised_for_the_service_to_report():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "errorType": "bad_data", "error": "parse error"})

    with pytest.raises(RuntimeError, match="bad_data"):
        asyncio.run(_prom(handler).fetch(QUERY, domain="metrics"))


class _FakeAws(AwsTopologyPlugin):
    RESPONSES = {
        "describe_vpn_connections": {"VpnConnections": [{
            "VpnConnectionId": "vpn-1", "State": "available", "TransitGatewayId": "tgw-1",
            "Options": {"TunnelOptions": [{"OutsideIpAddress": "3.3.3.1", "TunnelInsideCidr": "169.254.100.0/30"},
                                          {"OutsideIpAddress": "3.3.3.2", "TunnelInsideCidr": "169.254.100.4/30"}]}}]},
        "describe_transit_gateway_route_tables": [{"TransitGatewayRouteTables": [{"TransitGatewayRouteTableId": "tgw-rtb-1", "TransitGatewayId": "tgw-1"}]}],
        "describe_transit_gateway_attachments": [{"TransitGatewayAttachments": [
            {"TransitGatewayAttachmentId": "tgw-attach-v", "TransitGatewayId": "tgw-1", "ResourceType": "vpn", "ResourceId": "vpn-1", "State": "available"},
            {"TransitGatewayAttachmentId": "tgw-attach-a", "TransitGatewayId": "tgw-1", "ResourceType": "vpc", "ResourceId": "vpc-a",
             "State": "available", "Association": {"TransitGatewayRouteTableId": "tgw-rtb-1"}}]}],
        "get_transit_gateway_route_table_propagations": [{"TransitGatewayRouteTablePropagations": [
            {"TransitGatewayAttachmentId": "tgw-attach-v", "State": "enabled"}]}],
        "describe_vpcs": [{"Vpcs": [{"VpcId": "vpc-a", "CidrBlock": "10.10.0.0/16", "Tags": [{"Key": "Name", "Value": "app"}]}]}],
        "describe_instances": [{"Reservations": [{"Instances": [{"InstanceId": "i-1", "VpcId": "vpc-a", "State": {"Name": "running"},
                                                                "Tags": [{"Key": "Name", "Value": "netcortex-lab-app-a"}]}]}]}],
        "describe_subnets": [{"Subnets": []}], "describe_nat_gateways": [{"NatGateways": []}],
        "describe_vpc_peering_connections": [{"VpcPeeringConnections": []}],
        "describe_route_tables": [{"RouteTables": [{"RouteTableId": "rtb-1", "VpcId": "vpc-a", "Associations": [],
                                                    "Routes": [{"TransitGatewayId": "tgw-1"}]}]}],
        "describe_network_acls": [{"NetworkAcls": []}],
        "describe_security_groups": [{"SecurityGroups": [{"GroupId": "sg-1", "VpcId": "vpc-a"}]}],
        "describe_network_interfaces": [{"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "VpcId": "vpc-a",
                                                                "Groups": [{"GroupId": "sg-1"}], "Attachment": {"InstanceId": "i-1"}}]}],
    }

    async def call(self, service, region, operation, **params):
        return self.RESPONSES[operation]

    async def paginate(self, service, region, operation, *, max_pages, **params):
        if operation in ("describe_load_balancers", "list_hosted_zones"):
            raise RuntimeError("AccessDenied")
        return self.RESPONSES[operation], False


def test_aws_topology_matches_the_ids_other_plugins_use():
    plugin = _FakeAws({"region": "us-east-1", "flows": [
        {"id": "onprem-iperf-app-a", "probes": [{"type": "instance", "match": "netcortex-lab-app-a"}, {"type": "tunnel", "all": True}]}]})
    entities, edges, coverage = asyncio.run(plugin.topology("us-east-1", None))
    assert coverage.status == "partial" and "AccessDenied" in coverage.error  # elb/dns failed, the rest stands
    kinds = {(e.type, e.source.id, e.target.id) for e in edges}
    assert ("runs_over", "169.254.100.1", "vpn-1-tunnel-1") in kinds  # AWS inside IP = what the router calls its neighbor
    assert ("propagates_to", "tgw-attach-v", "tgw-rtb-1") in kinds
    assert ("applies_to", "sg-1", "eni-1") in kinds and ("attached_to", "eni-1", "i-1") in kinds
    assert ("probes", "onprem-iperf-app-a", "i-1") in kinds and ("probes", "onprem-iperf-app-a", "vpn-1-tunnel-2") in kinds

    graph = TopologyGraph.build(TopologyResponse(region="us-east-1", entities=entities, relationships=edges), get_catalog())
    assert graph.resolve("tunnel", "3.3.3.2") == ("tunnel", "vpn-1-tunnel-2")  # CloudWatch's tunnel id
    assert graph.depends_on(("flow", "onprem-iperf-app-a"), ("tgw_route_table", "tgw-rtb-1"), 5)
