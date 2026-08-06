"""MCP Telemetry Server — exposes simulation scenario data as MCP tools.

This server wraps the existing SCENARIOS data and serves it over the MCP
protocol (2026-07-28 spec, Streamable HTTP). Domain agents in the RCA pod
connect to this server via MCP client adapters.

In production, this would be replaced by real Prometheus/ELK/Splunk-backed
MCP servers. For the demo, it serves the same simulation data.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow imports when run as standalone module from project root.
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp.server import MCPServer

from simulation.scenarios import SCENARIOS

telemetry = MCPServer("NetCortex-Telemetry")


@telemetry.tool()
def get_metrics(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch network metrics telemetry for a region and time window.

    Returns time-series metric snapshots including error_rate, packet_loss,
    throughput_gbps, and latency_ms for network entities in the given region.
    """
    scenario = SCENARIOS.get(scenario_id, SCENARIOS.get(1))
    if scenario is None:
        return {"metrics": [], "error": f"Unknown scenario_id: {scenario_id}"}

    metrics = [
        m.model_dump(mode="json")
        for m in scenario.metrics_data
        if m.region == region
    ]
    return {"metrics": metrics, "count": len(metrics), "region": region}


@telemetry.tool()
def get_logs(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch log events for a region and time window.

    Returns log entries with level, service, and message fields.
    """
    scenario = SCENARIOS.get(scenario_id, SCENARIOS.get(1))
    if scenario is None:
        return {"logs": [], "error": f"Unknown scenario_id: {scenario_id}"}

    logs = [l.model_dump(mode="json") for l in scenario.log_events]
    return {"logs": logs, "count": len(logs), "region": region}


@telemetry.tool()
def get_routing_events(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch routing change events for a region and time window.

    Returns routing events including reroutes, flaps, congestion, and BGP updates.
    """
    scenario = SCENARIOS.get(scenario_id, SCENARIOS.get(1))
    if scenario is None:
        return {"routing_events": [], "error": f"Unknown scenario_id: {scenario_id}"}

    events = [
        r.model_dump(mode="json")
        for r in scenario.routing_events
        if r.region == region
    ]
    return {"routing_events": events, "count": len(events), "region": region}


@telemetry.tool()
def get_config_changes(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch configuration changes for a region and time window.

    Returns config change records including component, change_type, and before/after state.
    """
    scenario = SCENARIOS.get(scenario_id, SCENARIOS.get(1))
    if scenario is None:
        return {"config_changes": [], "error": f"Unknown scenario_id: {scenario_id}"}

    changes = [c.model_dump(mode="json") for c in scenario.config_changes]
    return {"config_changes": changes, "count": len(changes), "region": region}
