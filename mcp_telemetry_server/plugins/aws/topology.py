"""AWS network topology for get_topology, from read-only describe calls.

Config (sources.aws_topology):

    vpc_ids: []                       # empty = every VPC in the region
    tunnel_capacity_bps: 1250000000   # AWS Site-to-Site VPN per-tunnel limit, as labels.capacity_bps
    hosted_zones: true                # private hosted zones and their record names
    load_balancers: true
    cache_seconds: 300                # topology changes rarely; one read serves many incidents
    flows:                            # synthetic probes are not in any AWS API: say what each one tests
      - {id: onprem-to-nlb, probes: [{type: load_balancer, match: netcortex-lab-app}, {type: tunnel, all: true}]}
      - {id: svc-to-app, probes: [{type: dns_name, id: app.lab.internal.}]}

A probe names its target by `id`, by `match` (the id or any label value, such as
a Name tag, private IP or CloudWatch dimension), or `all: true` for every
entity of that type.

Entities use the ids the other plugins emit, and keep the other names as
labels so collaboration can match either:
- tunnel: `<vpn id>-tunnel-<n>`, with labels outside_ip and capacity_bps
- bgp_neighbor: the AWS inside IP, which is what the customer-gateway router
  calls its neighbor (host 1 of the tunnel inside CIDR)
- target_group and load_balancer: the ARN, with the CloudWatch dimension
  (`targetgroup/...`, `net/...`) as a label
- hosted_zone: the bare zone id

Relationships are the ones contract/catalog/entities.json defines.

Reads: ec2:DescribeVpnConnections, ec2:DescribeInstances, ec2:DescribeTransitGatewayAttachments,
ec2:DescribeTransitGatewayRouteTables, ec2:GetTransitGatewayRouteTablePropagations,
ec2:DescribeVpcs, ec2:DescribeSubnets, ec2:DescribeRouteTables, ec2:DescribeNatGateways,
ec2:DescribeVpcPeeringConnections, ec2:DescribeSecurityGroups, ec2:DescribeNetworkInterfaces,
ec2:DescribeNetworkAcls, elasticloadbalancing:DescribeLoadBalancers,
elasticloadbalancing:DescribeTargetGroups, elasticloadbalancing:DescribeTargetHealth,
route53:ListHostedZones, route53:ListResourceRecordSets.
"""

from __future__ import annotations

import ipaddress
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contract import Coverage, Entity, TopologyEdge
from mcp_telemetry_server.plugins.aws.base import AWS_REGION, AwsPlugin
from mcp_telemetry_server.plugins.base import register_plugin


class _Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    id: str | None = None
    match: str | None = Field(default=None, description="The id or any label value (Name tag, IP, dimension)")
    all: bool = False


class _Flow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    probes: list[_Probe] = Field(min_length=1)


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vpc_ids: list[str] = Field(default_factory=list)
    tunnel_capacity_bps: int = Field(default=1_250_000_000, ge=1)
    hosted_zones: bool = True
    load_balancers: bool = True
    cache_seconds: int = Field(default=300, ge=0)
    flows: list[_Flow] = Field(default_factory=list)


class _Graph:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], Entity] = {}
        self.edges: dict[tuple, TopologyEdge] = {}

    def add(self, etype: str, eid: str, **labels: Any) -> Entity:
        clean = {k: str(v) for k, v in labels.items() if v not in (None, "")}
        known = self.entities.get((etype, eid))
        entity = Entity(type=etype, id=eid, labels={**(known.labels if known else {}), **clean})
        self.entities[(etype, eid)] = entity
        return entity

    def link(self, kind: str, src: tuple[str, str], dst: tuple[str, str]) -> None:
        if src not in self.entities or dst not in self.entities:
            return
        self.edges[(kind, *src, *dst)] = TopologyEdge(type=kind, source=self.entities[src], target=self.entities[dst])

    def find(self, etype: str, value: str) -> list[tuple[str, str]]:
        return [(t, i) for (t, i), e in self.entities.items() if t == etype and (i == value or value in e.labels.values())]


def _aws_inside_ip(cidr: str | None) -> str | None:
    try:
        return str(next(ipaddress.ip_network(cidr).hosts())) if cidr else None
    except ValueError:
        return None


def _name(obj: dict[str, Any]) -> str | None:
    return next((t["Value"] for t in obj.get("Tags", []) if t.get("Key") == "Name"), None)


def _arn_suffix(arn: str, marker: str) -> str:
    return arn.split(marker, 1)[1] if marker in arn else arn


@register_plugin
class AwsTopologyPlugin(AwsPlugin):
    name = "aws_topology"
    domains = frozenset({"topology"})

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        try:
            self._cfg = _Config.model_validate(self.config)
        except ValueError as exc:
            raise ValueError(f"ConfigValidationError: sources.aws_topology: {exc}") from None
        self._cache: dict[str, tuple[float, tuple]] = {}

    async def fetch(self, query, *, domain):  # pragma: no cover - serves topology only
        raise NotImplementedError("aws_topology serves get_topology only")

    async def _pages(self, service: str, region: str, op: str, key: str, **params: Any) -> list[dict[str, Any]]:
        pages, _ = await self.paginate(service, region, op, max_pages=50, **params)
        return [item for page in pages for item in page.get(key, [])]

    async def topology(self, region: str, scenario_id: int | None) -> tuple[list[Entity], list[TopologyEdge], Coverage]:
        region = self._fixed_region or region
        if not AWS_REGION.match(region):
            raise ValueError(f"region '{region}' is not an AWS region; set sources.aws_topology.region")
        cached = self._cache.get(region)
        if cached is not None and time.monotonic() - cached[0] < self._cfg.cache_seconds:
            return cached[1]
        g = _Graph()
        errors: list[str] = []
        vpc_filter = [{"Name": "vpc-id", "Values": self._cfg.vpc_ids}] if self._cfg.vpc_ids else []

        async def step(name: str, coro) -> None:
            try:
                await coro
            except Exception as exc:  # one failed describe keeps the rest of the graph
                errors.append(f"{name}: {type(exc).__name__}: {exc}"[:200])

        async def vpn() -> None:
            for conn in (await self.call("ec2", region, "describe_vpn_connections")).get("VpnConnections", []):
                if conn.get("State") == "deleted":
                    continue
                vid = conn["VpnConnectionId"]
                g.add("vpn_connection", vid, customer_gateway=conn.get("CustomerGatewayId"),
                      transit_gateway=conn.get("TransitGatewayId"))
                options = (conn.get("Options") or {}).get("TunnelOptions") or []
                outside = [t.get("OutsideIpAddress") for t in options] or [t.get("OutsideIpAddress") for t in conn.get("VgwTelemetry", [])]
                for n, ip in enumerate(outside, start=1):
                    tid = f"{vid}-tunnel-{n}"
                    g.add("tunnel", tid, outside_ip=ip, capacity_bps=self._cfg.tunnel_capacity_bps)
                    g.link("member_of", ("tunnel", tid), ("vpn_connection", vid))
                    inside = _aws_inside_ip(options[n - 1].get("TunnelInsideCidr")) if n <= len(options) else None
                    if inside:
                        g.add("bgp_neighbor", inside, tunnel=tid)
                        g.link("runs_over", ("bgp_neighbor", inside), ("tunnel", tid))

        async def tgw() -> None:
            for rtb in await self._pages("ec2", region, "describe_transit_gateway_route_tables", "TransitGatewayRouteTables"):
                g.add("transit_gateway", rtb["TransitGatewayId"])
                g.add("tgw_route_table", rtb["TransitGatewayRouteTableId"], transit_gateway=rtb["TransitGatewayId"])
                g.link("belongs_to", ("tgw_route_table", rtb["TransitGatewayRouteTableId"]), ("transit_gateway", rtb["TransitGatewayId"]))
            for att in await self._pages("ec2", region, "describe_transit_gateway_attachments", "TransitGatewayAttachments"):
                if att.get("State") in ("deleted", "deleting"):
                    continue
                aid, kind, rid = att["TransitGatewayAttachmentId"], att.get("ResourceType"), att.get("ResourceId")
                g.add("tgw_attachment", aid, resource_type=kind, resource_id=rid)
                g.add("transit_gateway", att["TransitGatewayId"])
                g.link("belongs_to", ("tgw_attachment", aid), ("transit_gateway", att["TransitGatewayId"]))
                if kind == "vpn" and rid:
                    g.add("vpn_connection", rid)
                    g.link("attached_via", ("vpn_connection", rid), ("tgw_attachment", aid))
                elif kind == "vpc" and rid:
                    g.add("vpc", rid)
                    g.link("attached_via", ("vpc", rid), ("tgw_attachment", aid))
                assoc = (att.get("Association") or {}).get("TransitGatewayRouteTableId")
                if assoc:
                    g.link("associated_with", ("tgw_attachment", aid), ("tgw_route_table", assoc))
            for (etype, rtb_id) in [k for k in g.entities if k[0] == "tgw_route_table"]:
                props = await self._pages("ec2", region, "get_transit_gateway_route_table_propagations",
                                          "TransitGatewayRouteTablePropagations", TransitGatewayRouteTableId=rtb_id)
                for prop in props:
                    if prop.get("State") == "enabled":
                        g.link("propagates_to", ("tgw_attachment", prop["TransitGatewayAttachmentId"]), (etype, rtb_id))

        async def vpcs() -> None:
            for vpc in await self._pages("ec2", region, "describe_vpcs", "Vpcs", Filters=vpc_filter):
                g.add("vpc", vpc["VpcId"], cidr=vpc.get("CidrBlock"), name=_name(vpc))
            for reservation in await self._pages("ec2", region, "describe_instances", "Reservations", Filters=vpc_filter):
                for inst in reservation.get("Instances", []):
                    if (inst.get("State") or {}).get("Name") == "terminated":
                        continue
                    g.add("instance", inst["InstanceId"], name=_name(inst), private_ip=inst.get("PrivateIpAddress"),
                          vpc=inst.get("VpcId"))
                    g.link("in_vpc", ("instance", inst["InstanceId"]), ("vpc", inst.get("VpcId", "")))
            for sub in await self._pages("ec2", region, "describe_subnets", "Subnets", Filters=vpc_filter):
                g.add("subnet", sub["SubnetId"], vpc=sub["VpcId"], cidr=sub.get("CidrBlock"))
                g.link("in_vpc", ("subnet", sub["SubnetId"]), ("vpc", sub["VpcId"]))
            for nat in await self._pages("ec2", region, "describe_nat_gateways", "NatGateways", Filter=vpc_filter):
                if nat.get("State") in ("deleted", "deleting"):
                    continue
                g.add("nat_gateway", nat["NatGatewayId"], vpc=nat.get("VpcId"))
                g.link("in_vpc", ("nat_gateway", nat["NatGatewayId"]), ("vpc", nat.get("VpcId", "")))
            for pcx in await self._pages("ec2", region, "describe_vpc_peering_connections", "VpcPeeringConnections"):
                if (pcx.get("Status") or {}).get("Code") != "active":
                    continue
                req, acc = pcx["RequesterVpcInfo"].get("VpcId"), pcx["AccepterVpcInfo"].get("VpcId")
                g.add("vpc_peering", pcx["VpcPeeringConnectionId"], requester=req, accepter=acc)
                for vpc_id in (req, acc):
                    if vpc_id and ("vpc", vpc_id) in g.entities:
                        g.link("peers", ("vpc", vpc_id), ("vpc_peering", pcx["VpcPeeringConnectionId"]))
            for rt in await self._pages("ec2", region, "describe_route_tables", "RouteTables", Filters=vpc_filter):
                rid = rt["RouteTableId"]
                g.add("route_table", rid, vpc=rt.get("VpcId"))
                g.link("in_vpc", ("route_table", rid), ("vpc", rt.get("VpcId", "")))
                for assoc in rt.get("Associations", []):
                    if assoc.get("SubnetId"):
                        g.link("associated_with", ("subnet", assoc["SubnetId"]), ("route_table", rid))
                for route in rt.get("Routes", []):
                    for key, etype in (("NatGatewayId", "nat_gateway"), ("VpcPeeringConnectionId", "vpc_peering"),
                                       ("TransitGatewayId", "transit_gateway"), ("InstanceId", "instance"),
                                       ("NetworkInterfaceId", "eni")):
                        if route.get(key):
                            g.add(etype, route[key])
                            g.link("routes_via", ("route_table", rid), (etype, route[key]))
            for acl in await self._pages("ec2", region, "describe_network_acls", "NetworkAcls", Filters=vpc_filter):
                g.add("network_acl", acl["NetworkAclId"], vpc=acl.get("VpcId"))
                for assoc in acl.get("Associations", []):
                    g.link("protects", ("network_acl", acl["NetworkAclId"]), ("subnet", assoc.get("SubnetId", "")))
            for sg in await self._pages("ec2", region, "describe_security_groups", "SecurityGroups", Filters=vpc_filter):
                g.add("security_group", sg["GroupId"], name=sg.get("GroupName"), vpc=sg.get("VpcId"))
                g.link("in_vpc", ("security_group", sg["GroupId"]), ("vpc", sg.get("VpcId", "")))
            for eni in await self._pages("ec2", region, "describe_network_interfaces", "NetworkInterfaces", Filters=vpc_filter):
                eid = eni["NetworkInterfaceId"]
                g.add("eni", eid, subnet=eni.get("SubnetId"), private_ip=eni.get("PrivateIpAddress"))
                for grp in eni.get("Groups", []):
                    g.link("applies_to", ("security_group", grp["GroupId"]), ("eni", eid))
                instance = (eni.get("Attachment") or {}).get("InstanceId")
                if instance:
                    g.add("instance", instance, vpc=eni.get("VpcId"))
                    g.link("attached_to", ("eni", eid), ("instance", instance))
                    g.link("in_vpc", ("instance", instance), ("vpc", eni.get("VpcId", "")))

        async def elb() -> None:
            for lb in await self._pages("elbv2", region, "describe_load_balancers", "LoadBalancers"):
                arn = lb["LoadBalancerArn"]
                g.add("load_balancer", arn, dimension=_arn_suffix(arn, ":loadbalancer/"), name=lb.get("LoadBalancerName"))
                g.link("in_vpc", ("load_balancer", arn), ("vpc", lb.get("VpcId", "")))
            for tg in await self._pages("elbv2", region, "describe_target_groups", "TargetGroups"):
                arn = tg["TargetGroupArn"]
                g.add("target_group", arn, dimension="targetgroup/" + _arn_suffix(arn, ":targetgroup/"))
                for lb_arn in tg.get("LoadBalancerArns", []):
                    g.link("serves", ("target_group", arn), ("load_balancer", lb_arn))
                health = await self.call("elbv2", region, "describe_target_health", TargetGroupArn=arn)
                for desc in health.get("TargetHealthDescriptions", []):
                    target = (desc.get("Target") or {}).get("Id", "")
                    if target.startswith("i-"):
                        g.add("instance", target)
                        g.link("registered_in", ("instance", target), ("target_group", arn))

        async def dns() -> None:
            for zone in await self._pages("route53", "us-east-1", "list_hosted_zones", "HostedZones"):
                if not (zone.get("Config") or {}).get("PrivateZone"):
                    continue
                zid = zone["Id"].rsplit("/", 1)[-1]
                g.add("hosted_zone", zid, name=zone.get("Name"))
                for rec in await self._pages("route53", "us-east-1", "list_resource_record_sets", "ResourceRecordSets", HostedZoneId=zid):
                    if rec.get("Type") in ("A", "AAAA", "CNAME"):
                        g.add("dns_name", rec["Name"])
                        g.link("resolves", ("hosted_zone", zid), ("dns_name", rec["Name"]))

        # Order matters for links: VPN and VPC entities exist before attachments link to them.
        await step("vpn", vpn())
        await step("vpcs", vpcs())
        await step("tgw", tgw())
        if self._cfg.load_balancers:
            await step("elb", elb())
        if self._cfg.hosted_zones:
            await step("dns", dns())
        for flow in self._cfg.flows:
            g.add("flow", flow.id)
            for probe in flow.probes:
                if probe.all:
                    targets = [k for k in g.entities if k[0] == probe.type]
                elif probe.id:
                    targets = [(probe.type, probe.id)]
                    if targets[0] not in g.entities:
                        g.add(probe.type, probe.id)
                else:
                    targets = g.find(probe.type, probe.match or "")
                if not targets:
                    errors.append(f"flow {flow.id}: no {probe.type} matches {probe.id or probe.match}")
                for target in targets:
                    g.link("probes", ("flow", flow.id), target)

        status = "partial" if errors else "ok"
        result = (list(g.entities.values()), list(g.edges.values()), self.coverage(status, error="; ".join(errors) or None))
        if not errors:
            self._cache[region] = (time.monotonic(), result)
        return result
