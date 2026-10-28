"""CloudTrail management events -> config-domain events.

Config (sources.cloudtrail):

    event_sources: [ec2.amazonaws.com, elasticloadbalancing.amazonaws.com, route53.amazonaws.com]
    include_read_only: false
    max_events: 500

Reads event history with LookupEvents (no trail needed; about 5 minutes of
delivery lag) and keeps only calls from `event_sources`. The allow-list is
also what keeps out events that would describe the lab's fault tooling to
NetCortex, such as ssm SendCommand comments or logs calls on the faults log
group. The signal name is the API call (`eventName`), which signals.json
maps to `cloudtrail_network_mutation`. Failed calls are kept at debug
severity: they changed nothing.

Route 53 is a global service whose events are recorded in us-east-1.

Reads: cloudtrail:LookupEvents (2 requests/second per account and region).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contract import Coverage, Entity, Signal, TelemetryQuery
from mcp_telemetry_server.plugins.aws.base import AwsPlugin, now_utc, utc
from mcp_telemetry_server.plugins.base import register_plugin

DEFAULT_EVENT_SOURCES = ["ec2.amazonaws.com", "elasticloadbalancing.amazonaws.com", "route53.amazonaws.com"]
MAX_REQUEST_CHARS = 1000
PAGE_SIZE = 50  # LookupEvents maximum
LOOKUP_CACHE_SECONDS = 60.0

# Request parameters that identify the resource a call changed, in priority
# order, matched case-insensitively at any depth (newer EC2 APIs wrap their
# parameters in "<Operation>Request" with PascalCase keys).
_ENTITY_KEYS: list[tuple[str, str]] = [
    ("transitGatewayRouteTableId", "tgw_route_table"),
    ("transitGatewayAttachmentId", "tgw_attachment"),
    ("vpnConnectionId", "vpn_connection"),
    ("groupId", "security_group"),
    ("networkAclId", "network_acl"),
    ("routeTableId", "route_table"),
    ("vpcPeeringConnectionId", "vpc_peering"),
    ("networkInterfaceId", "eni"),
    ("instanceId", "instance"),
    ("hostedZoneId", "hosted_zone"),
    ("targetGroupArn", "target_group"),
    ("loadBalancerArn", "load_balancer"),
    ("transitGatewayId", "transit_gateway"),
    ("vpcId", "vpc"),
]

_RESOURCE_TYPES = {
    "AWS::EC2::SecurityGroup": "security_group",
    "AWS::EC2::NetworkAcl": "network_acl",
    "AWS::EC2::RouteTable": "route_table",
    "AWS::EC2::TransitGatewayRouteTable": "tgw_route_table",
    "AWS::EC2::TransitGatewayAttachment": "tgw_attachment",
    "AWS::EC2::VPNConnection": "vpn_connection",
    "AWS::EC2::Instance": "instance",
    "AWS::EC2::VPC": "vpc",
    "AWS::Route53::HostedZone": "hosted_zone",
}


def _find_key(value: Any, key: str) -> str | None:
    want = key.lower()
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() == want and isinstance(v, (str, int)):
                return str(v)
        for v in value.values():
            found = _find_key(v, key)
            if found:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _find_key(v, key)
            if found:
                return found
    return None


def _entity(request: Any, resources: list[dict[str, Any]], event_source: str) -> Entity:
    for key, kind in _ENTITY_KEYS:
        found = _find_key(request, key)
        if found:
            return Entity(type=kind, id=found.rsplit("/", 1)[-1] if kind == "hosted_zone" else found)
    for res in resources:
        kind = _RESOURCE_TYPES.get(res.get("ResourceType", ""))
        if kind and res.get("ResourceName"):
            return Entity(type=kind, id=res["ResourceName"])
    return Entity(type="aws_service", id=event_source)


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_sources: list[str] = Field(default_factory=lambda: list(DEFAULT_EVENT_SOURCES), min_length=1)
    include_read_only: bool = False
    max_events: int = Field(default=500, ge=1)


@register_plugin
class CloudTrailPlugin(AwsPlugin):
    name = "cloudtrail"
    domains = frozenset({"config"})

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        try:
            self._cfg = _Config.model_validate(self.config)
        except ValueError as exc:
            raise ValueError(f"ConfigValidationError: sources.cloudtrail: {exc}") from None
        self._sources = set(self._cfg.event_sources)
        # LookupEvents allows 2 requests/s per account. Collaboration asks the
        # config domain many scoped questions over the same window, and the
        # entity filter is applied after the plugin, so identical windows are
        # read once (coalesced while in flight, then cached briefly) and reads
        # go out one at a time.
        self._lookups: dict[tuple, tuple[float, asyncio.Future]] = {}
        self._one_at_a_time: asyncio.Lock | None = None

    async def _lookup(self, region: str, params: dict[str, Any], max_pages: int) -> tuple[list[dict[str, Any]], bool]:
        key = (region, params["StartTime"], params["EndTime"])
        now = time.monotonic()
        self._lookups = {k: v for k, v in self._lookups.items() if now - v[0] < LOOKUP_CACHE_SECONDS}
        cached = self._lookups.get(key)
        if cached is not None:
            return await asyncio.shield(cached[1])
        if self._one_at_a_time is None:
            self._one_at_a_time = asyncio.Lock()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._lookups[key] = (now, future)
        try:
            async with self._one_at_a_time:
                result = await self.paginate("cloudtrail", region, "lookup_events", max_pages=max_pages, **params)
        except BaseException as exc:
            self._lookups.pop(key, None)
            future.set_exception(exc)
            future.exception()  # retrieved: waiters re-raise it, nobody else has to
            raise
        future.set_result(result)
        return result

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        region = self.region_for(query)
        params: dict[str, Any] = {"StartTime": query.start_time, "EndTime": query.end_time}
        if not self._cfg.include_read_only:
            params["LookupAttributes"] = [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}]
        max_pages = -(-self._cfg.max_events // PAGE_SIZE)
        pages, truncated = await self._lookup(region, params, max_pages)
        observed = now_utc()

        signals: list[Signal] = []
        for page in pages:
            for event in page.get("Events", []):
                try:
                    body = json.loads(event.get("CloudTrailEvent") or "{}")
                except ValueError:
                    body = {}
                source = event.get("EventSource") or body.get("eventSource", "")
                if source not in self._sources:
                    continue
                ts = utc(event["EventTime"])
                request = body.get("requestParameters") or {}
                error_code = body.get("errorCode")
                read_only = str(event.get("ReadOnly", body.get("readOnly", "false"))).lower() == "true"
                signals.append(Signal(
                    signal_type="event", timestamp=ts, observed_at=observed,
                    entity=_entity(request, event.get("Resources") or [], source),
                    name=event["EventName"], severity="debug" if error_code else "info",
                    domain="config", source=self.name,
                    attributes={
                        "event_source": source, "read_only": read_only, "region": region,
                        "username": event.get("Username"), "error_code": error_code,
                        "request": json.dumps(request, sort_keys=True)[:MAX_REQUEST_CHARS],
                        "raw": {"EventId": event.get("EventId"), "EventName": event["EventName"]},
                    },
                    evidence_ref=f"cloudtrail:{event.get('EventId')}",
                ))
        signals.sort(key=lambda s: s.timestamp)
        error = f"event cap max_events={self._cfg.max_events} hit" if truncated else None
        return signals, self.coverage("partial" if truncated else "ok", truncated=truncated, error=error)
