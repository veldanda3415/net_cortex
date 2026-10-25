"""Simulation source: serves simulation/scenarios.py as contract v2 signals.

Field mapping (which scenario fields become which signals, entities and
attributes) comes from the catalog's sources.json entries with
`"source": "simulation"`; this module only reads the scenario bundle,
rebases time and filters by region/window.

Scenario timestamps are stored relative to when the scenarios module was
imported, so they are rebased onto the query window (the bundle's own
``incident_request.reported_at`` maps to ``query.end_time``, ages rounded
to whole seconds). That keeps a
long-running server returning data inside the requested window instead of
drifting out of it after ``window`` minutes of uptime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from contract import Coverage, Entity, Signal, TelemetryQuery, TopologyEdge
from contract.catalog_loader import get_catalog
from mcp_telemetry_server.mapping import map_record
from mcp_telemetry_server.plugins.base import SourcePlugin, register_plugin
from models.schemas import ScenarioDataBundle
from simulation.scenarios import SCENARIOS


def _static_topology(region: str) -> tuple[list[Entity], list[TopologyEdge]]:
    """A small hybrid-connectivity topology covering every relationship the
    get_topology tool documents, so consumers can be built against it before
    real describe-* calls exist."""

    def e(kind: str, ident: str, **labels: str) -> Entity:
        return Entity(type=kind, id=ident, labels={"region": region, **labels})

    tunnel_1 = e("tunnel", "vpn-0a1b2c3d-tunnel-1", outside_ip="203.0.113.10")
    tunnel_2 = e("tunnel", "vpn-0a1b2c3d-tunnel-2", outside_ip="203.0.113.11")
    vpn = e("vpn_connection", "vpn-0a1b2c3d", customer_gateway="cgw-0f00ba11")
    vpn_att = e("tgw_attachment", "tgw-attach-0vpn0001", resource_type="vpn")
    vpc_att = e("tgw_attachment", "tgw-attach-0vpc0001", resource_type="vpc")
    tgw_rt = e("tgw_route_table", "tgw-rtb-0core0001", transit_gateway="tgw-0core0001")
    vpc_a = e("vpc", "vpc-0app00001", cidr="10.20.0.0/16")
    vpc_b = e("vpc", "vpc-0shared001", cidr="10.30.0.0/16")
    subnet = e("subnet", "subnet-0app00001", cidr="10.20.1.0/24", vpc=vpc_a.id)
    route_table = e("route_table", "rtb-0app00001", vpc=vpc_a.id)
    sg = e("security_group", "sg-0app00001", vpc=vpc_a.id)
    eni = e("eni", "eni-0app00001", subnet=subnet.id)
    instance = e("instance", "i-0app000001", subnet=subnet.id)
    peering = e("vpc_peering", "pcx-0app2shared", requester=vpc_a.id, accepter=vpc_b.id)

    def edge(kind: str, src: Entity, dst: Entity) -> TopologyEdge:
        return TopologyEdge(type=kind, source=src, target=dst)

    entities = [tunnel_1, tunnel_2, vpn, vpn_att, vpc_att, tgw_rt, vpc_a, vpc_b, subnet, route_table, sg, eni, instance, peering]
    edges = [
        edge("member_of", tunnel_1, vpn),
        edge("member_of", tunnel_2, vpn),
        edge("attached_via", vpn, vpn_att),
        edge("associated_with", vpn_att, tgw_rt),
        edge("associated_with", vpc_att, tgw_rt),
        edge("attached_via", vpc_a, vpc_att),
        edge("applies_to", sg, eni),
        edge("attached_to", eni, instance),
        edge("associated_with", subnet, route_table),
        edge("peers", vpc_a, peering),
        edge("peers", peering, vpc_b),
    ]
    return entities, edges


@register_plugin
class SimulationPlugin(SourcePlugin):
    name = "simulation"
    domains = frozenset({"*"})  # narrowed per instance to the domains sources.json maps

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._mappings = get_catalog().mappings_for("simulation")
        mapped = {m.domain for m in self._mappings.values()} | {"topology"}
        if "domains" not in self.config:
            self.domains = frozenset(mapped)

    def _bundle(self, scenario_id: int | None) -> ScenarioDataBundle:
        sid = scenario_id or 1
        bundle = SCENARIOS.get(sid)
        if bundle is None:
            raise ValueError(f"Unknown scenario_id: {sid}")
        return bundle

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        bundle = self._bundle(query.scenario_id)
        sid = bundle.scenario_id
        anchor = bundle.incident_request.reported_at.astimezone(timezone.utc)

        def rebase(ts: datetime) -> datetime:
            # Scenarios are authored as "N minutes before the incident", but
            # reported_at is stamped a few microseconds after the rows were
            # built. Rounding the age to whole seconds removes that drift, so a
            # row authored at exactly -window still lands on the window start.
            age = timedelta(seconds=round((anchor - ts.astimezone(timezone.utc)).total_seconds()))
            return query.end_time - age

        signals: list[Signal] = []
        for mapping in self._mappings.values():
            if mapping.domain != domain:
                continue
            rows = getattr(bundle, mapping.record, None)
            if rows is None:
                raise ValueError(f"sources.json mapping reads unknown scenario record '{mapping.record}'")
            ref_base = f"simulation:scenario/{sid}/{domain}"
            for idx, row in enumerate(rows):
                record = row.model_dump()
                # Records without a per-row region (logs, config changes) are
                # filtered at bundle granularity; every scenario is single-region.
                row_region = record.get(mapping.region_field) if mapping.region_field else bundle.incident_request.region
                if row_region != query.region:
                    continue
                signals.extend(map_record(mapping, record, timestamp=rebase(row.timestamp), evidence_ref=f"{ref_base}/{idx}"))

        in_window = [s for s in signals if query.start_time <= s.timestamp <= query.end_time]
        return in_window, self.coverage(lag_seconds=0.0)

    async def topology(self, region: str, scenario_id: int | None) -> tuple[list[Entity], list[TopologyEdge], Coverage]:
        entities, edges = _static_topology(region)
        return entities, edges, self.coverage(lag_seconds=0.0)

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "scenarios": len(SCENARIOS)}
