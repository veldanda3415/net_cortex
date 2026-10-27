"""Transit Gateway route tables -> `tgw_route` state signals.

Config (sources.tgw_routes):

    transit_gateway_ids: []          # empty = every TGW in the region
    states: [blackhole]              # route states to emit; the catalog rule fires on blackhole
    max_routes: 200                  # per route table
    snapshot_max_age_seconds: 900

Route state is only available as a current snapshot, not as history, so the
signal is stamped at the query's end time and only taken when that end time
is within `snapshot_max_age_seconds` of now. For an older window the source
reports `partial` with no signals instead of passing today's routes off as
the incident's. Route changes over time come from Network Manager events
(the cloudwatch_logs plugin, routing domain).

Only blackhole routes are emitted by default: an active route is not
evidence, and the routing domain lists its first observations as key events.

Reads: ec2:DescribeTransitGatewayRouteTables, ec2:SearchTransitGatewayRoutes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contract import Coverage, Entity, Signal, TelemetryQuery
from mcp_telemetry_server.plugins.aws.base import AwsPlugin, now_utc
from mcp_telemetry_server.plugins.base import register_plugin


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    transit_gateway_ids: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=lambda: ["blackhole"], min_length=1)
    max_routes: int = Field(default=200, ge=5, le=1000)
    snapshot_max_age_seconds: int = Field(default=900, ge=0)


@register_plugin
class TgwRoutesPlugin(AwsPlugin):
    name = "tgw_routes"
    domains = frozenset({"routing"})

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        try:
            self._cfg = _Config.model_validate(self.config)
        except ValueError as exc:
            raise ValueError(f"ConfigValidationError: sources.tgw_routes: {exc}") from None

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        observed = now_utc()
        age = (observed - query.end_time).total_seconds()
        if age > self._cfg.snapshot_max_age_seconds:
            return [], self.coverage(
                "partial", error=f"route snapshot skipped: window ended {age:.0f}s ago "
                f"(> snapshot_max_age_seconds={self._cfg.snapshot_max_age_seconds})",
            )
        region = self.region_for(query)
        params: dict[str, Any] = {}
        if self._cfg.transit_gateway_ids:
            params["Filters"] = [{"Name": "transit-gateway-id", "Values": self._cfg.transit_gateway_ids}]
        pages, _ = await self.paginate("ec2", region, "describe_transit_gateway_route_tables", max_pages=10, **params)
        tables = [t for p in pages for t in p.get("TransitGatewayRouteTables", []) if t.get("State") == "available"]

        stamp = min(observed, query.end_time)
        signals: list[Signal] = []
        truncated = False
        for table in tables:
            rtb = table["TransitGatewayRouteTableId"]
            result = await self.call(
                "ec2", region, "search_transit_gateway_routes", TransitGatewayRouteTableId=rtb,
                Filters=[{"Name": "state", "Values": self._cfg.states}], MaxResults=self._cfg.max_routes,
            )
            truncated = truncated or bool(result.get("AdditionalRoutesAvailable"))
            for route in result.get("Routes", []):
                cidr = route.get("DestinationCidrBlock") or route.get("PrefixListId", "")
                attachments = [a.get("TransitGatewayAttachmentId") for a in route.get("TransitGatewayAttachments", [])]
                signals.append(Signal(
                    signal_type="state", timestamp=stamp, observed_at=observed,
                    entity=Entity(type="tgw_route_table", id=rtb, labels={"cidr": cidr}),
                    name="tgw_route", severity="info", domain="routing", source=self.name,
                    attributes={
                        "state": route.get("State"), "destination_cidr": cidr, "route_type": route.get("Type"),
                        "attachments": attachments, "transit_gateway": table.get("TransitGatewayId"),
                        "api": "SearchTransitGatewayRoutes", "snapshot": True, "region": region,
                    },
                    evidence_ref=f"tgw_routes:{rtb}/{cidr}@{stamp.isoformat()}",
                ))
        error = f"route cap max_routes={self._cfg.max_routes} hit" if truncated else None
        return signals, self.coverage("partial" if truncated else "ok", truncated=truncated, error=error,
                                      lag_seconds=max(0.0, age))
