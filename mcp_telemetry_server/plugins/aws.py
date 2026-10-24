"""AWS source plugin skeletons — interface only, no boto3.

Each class pins down the plugin name, the domains it will serve, and (in its
docstring) the API calls and IAM actions the real implementation needs, so
the IAM policy for the telemetry pod can be reviewed before any code lands.
Enabling one today fails server startup with a clear error.
"""

from __future__ import annotations

from mcp_telemetry_server.plugins.base import SkeletonPlugin, register_plugin


@register_plugin
class CloudWatchPlugin(SkeletonPlugin):
    """CloudWatch metrics: VPN TunnelState/TunnelDataIn/Out, TGW
    BytesIn/PacketDropCount*, NAT/ELB error counters.

    Reads: cloudwatch:GetMetricData, cloudwatch:ListMetrics.
    Emits: signal_type=metric, name=<CloudWatch MetricName>, entity from the
    metric dimensions (e.g. type=tunnel id=<TunnelIpAddress>), evidence_ref =
    "<namespace>/<metric>/<dimensions-hash>@<timestamp>".
    """

    name = "cloudwatch"
    domains = frozenset({"metrics"})


@register_plugin
class CloudTrailPlugin(SkeletonPlugin):
    """CloudTrail management events touching network config
    (Modify/Replace/Delete on routes, SGs, NACLs, VPN, TGW).

    Reads: cloudtrail:LookupEvents.
    Emits: signal_type=event, name=<eventName>, domain=config,
    evidence_ref=<CloudTrail eventID>.
    """

    name = "cloudtrail"
    domains = frozenset({"config"})


@register_plugin
class TgwRoutesPlugin(SkeletonPlugin):
    """Transit Gateway route tables: route state (active/blackhole),
    propagations and associations; also serves topology edges for
    tgw_attachment -> tgw_route_table.

    Reads: ec2:SearchTransitGatewayRoutes, ec2:GetTransitGatewayRouteTableAssociations,
    ec2:GetTransitGatewayRouteTablePropagations, ec2:DescribeTransitGatewayAttachments.
    Emits: signal_type=state, name=tgw_route, entity type=tgw_route_table.
    """

    name = "tgw_routes"
    domains = frozenset({"routing", "topology"})


@register_plugin
class FlowLogsPlugin(SkeletonPlugin):
    """VPC / TGW Flow Logs via CloudWatch Logs Insights (REJECT spikes,
    top talkers, asymmetric flows).

    Reads: logs:StartQuery, logs:GetQueryResults.
    Emits: signal_type=metric (aggregates) or event (samples), evidence_ref =
    "<log group>/<query id>".
    """

    name = "flow_logs"
    domains = frozenset({"logs", "metrics"})


@register_plugin
class FrrSsmPlugin(SkeletonPlugin):
    """FRR/bgpd logs and `show bgp summary` from customer-gateway hosts, via
    SSM Run Command (no inbound SSH).

    Reads: ssm:SendCommand (AWS-RunShellScript, read-only commands only),
    ssm:GetCommandInvocation.
    Emits: signal_type=event for log lines (domain=logs), signal_type=state for
    BGP neighbor state (domain=routing), evidence_ref = "<command id>/<instance id>".
    """

    name = "frr_ssm"
    domains = frozenset({"logs", "routing"})
