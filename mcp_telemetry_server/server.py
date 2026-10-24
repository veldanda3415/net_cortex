"""MCP Telemetry Server — serves telemetry contract v2 over MCP.

Tools (all return contract v2 JSON, see CONTRACT.md):
  get_metrics / get_logs / get_routing_events / get_config_changes -> ToolResponse
  get_topology                                                    -> TopologyResponse

Each tool fans out to the source plugins enabled for its domain (see
mcp_telemetry_server/plugins/ and the `sources:` config section). HTTP
routes /health (per-plugin status) and /ready (503 until a plugin is
usable) back the Kubernetes probes.

All cloud credentials and IAM live here, never in the RCA pod.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow imports when run as standalone module from project root.
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from contract import CONTRACT_VERSION, ContractVersionError, TelemetryQuery, check_contract_version
from mcp_telemetry_server.service import TelemetryService, load_sources_config


def _validated_query(contract_version: str, **fields: Any) -> TelemetryQuery:
    try:
        check_contract_version(contract_version)
    except ContractVersionError as exc:
        raise ToolError(str(exc)) from exc
    try:
        return TelemetryQuery(**fields)
    except ValidationError as exc:
        raise ToolError(f"Invalid telemetry query: {exc}") from exc


def build_server(service: TelemetryService, name: str = "NetCortex-Telemetry") -> MCPServer:
    server = MCPServer(name)
    server.telemetry_service = service  # type: ignore[attr-defined]  (handy for tests/introspection)

    async def _domain_tool(
        domain: str,
        region: str,
        start_time: datetime,
        end_time: datetime,
        entity_filters: list[dict[str, Any]] | None,
        include_baseline: bool,
        scenario_id: int | None,
        contract_version: str,
    ) -> dict:
        query = _validated_query(
            contract_version,
            region=region,
            start_time=start_time,
            end_time=end_time,
            entity_filters=entity_filters,
            include_baseline=include_baseline,
            scenario_id=scenario_id,
        )
        response = await service.query(domain, query)
        return response.model_dump(mode="json")

    @server.tool()
    async def get_metrics(
        region: str,
        start_time: datetime,
        end_time: datetime,
        entity_filters: list[dict[str, Any]] | None = None,
        include_baseline: bool = False,
        scenario_id: int | None = None,
        contract_version: str = CONTRACT_VERSION,
    ) -> dict:
        """Metric signals (counters, gauges, rates) for a region in [start_time, end_time].

        Returns a contract v2 ToolResponse: window, coverage per source, signals.
        """
        return await _domain_tool("metrics", region, start_time, end_time, entity_filters,
                                  include_baseline, scenario_id, contract_version)

    @server.tool()
    async def get_logs(
        region: str,
        start_time: datetime,
        end_time: datetime,
        entity_filters: list[dict[str, Any]] | None = None,
        include_baseline: bool = False,
        scenario_id: int | None = None,
        contract_version: str = CONTRACT_VERSION,
    ) -> dict:
        """Log event signals for a region in [start_time, end_time]. Returns a contract v2 ToolResponse."""
        return await _domain_tool("logs", region, start_time, end_time, entity_filters,
                                  include_baseline, scenario_id, contract_version)

    @server.tool()
    async def get_routing_events(
        region: str,
        start_time: datetime,
        end_time: datetime,
        entity_filters: list[dict[str, Any]] | None = None,
        include_baseline: bool = False,
        scenario_id: int | None = None,
        contract_version: str = CONTRACT_VERSION,
    ) -> dict:
        """Routing change/state signals (reroutes, BGP, route tables) in [start_time, end_time].

        Returns a contract v2 ToolResponse.
        """
        return await _domain_tool("routing", region, start_time, end_time, entity_filters,
                                  include_baseline, scenario_id, contract_version)

    @server.tool()
    async def get_config_changes(
        region: str,
        start_time: datetime,
        end_time: datetime,
        entity_filters: list[dict[str, Any]] | None = None,
        include_baseline: bool = False,
        scenario_id: int | None = None,
        contract_version: str = CONTRACT_VERSION,
    ) -> dict:
        """Configuration change signals in [start_time, end_time]. Returns a contract v2 ToolResponse."""
        return await _domain_tool("config", region, start_time, end_time, entity_filters,
                                  include_baseline, scenario_id, contract_version)

    @server.tool()
    async def get_topology(
        region: str,
        scenario_id: int | None = None,
        contract_version: str = CONTRACT_VERSION,
    ) -> dict:
        """Entities and relationships for a region: tunnel -> vpn_connection -> tgw_attachment ->
        tgw_route_table, security_group -> eni -> instance, subnet -> route_table, vpc_peering.

        Returns a contract v2 TopologyResponse.
        """
        try:
            check_contract_version(contract_version)
        except ContractVersionError as exc:
            raise ToolError(str(exc)) from exc
        response = await service.topology(region, scenario_id)
        return response.model_dump(mode="json")

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        # Liveness: the process is serving; per-plugin detail is informational.
        return JSONResponse({"contract_version": CONTRACT_VERSION, **service.health()})

    @server.custom_route("/ready", methods=["GET"])
    async def ready(request: Request) -> JSONResponse:
        body = {"contract_version": CONTRACT_VERSION, **service.health()}
        return JSONResponse(body, status_code=503 if body["status"] == "down" else 200)

    return server


# Module-level default instance (sources from $NETCORTEX_TELEMETRY_CONFIG or
# simulation-only), kept so `from mcp_telemetry_server.server import telemetry`
# works for verify scripts and in-process use.
telemetry = build_server(TelemetryService(load_sources_config()))
