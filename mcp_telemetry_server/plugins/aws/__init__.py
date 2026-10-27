"""AWS source plugins. Importing this package registers them.

Each cloud or on-prem backend lives in its own package or module under
plugins/ (aws/ here; Prometheus/ELK/Splunk in legacy_backends.py) and emits
the same contract v2 signals. What a signal means is decided by the
detection catalog, not by the plugin.

    cloudwatch       metrics   CloudWatch metrics (GetMetricData)
    cloudwatch_logs  logs, routing   named events or counts from log groups (FilterLogEvents)
    cloudtrail       config    management events (LookupEvents)
    tgw_routes       routing   current TGW blackhole routes (SearchTransitGatewayRoutes)
    frr_ssm          logs, routing   skeleton; the lab ships FRR logs to CloudWatch instead
"""

from mcp_telemetry_server.plugins.aws import cloudtrail, cloudwatch, logs, tgw  # noqa: F401  (registration)
from mcp_telemetry_server.plugins.base import SkeletonPlugin, register_plugin


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
