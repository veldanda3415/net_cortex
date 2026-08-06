"""MCP RCA Server — exposes NetCortex RCA as MCP tools (2026-07-28 spec).

Tools:
  - run_rca: Trigger an RCA analysis (long-running, returns task handle)
  - get_report: Retrieve a completed RCA report
  - list_scenarios: List available simulation scenarios

The server is stateless per the 2026-07-28 spec. Long-running RCA runs are
tracked via the Tasks extension (io.modelcontextprotocol/tasks). Client polls
with tasks/get for progress and completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure project root on path for imports.
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp.server import MCPServer

from mcp_server.tasks import TaskStatus, TaskStore
from models.schemas import IncidentRequest, RCAReport
from simulation.scenarios import SCENARIOS

logger = logging.getLogger("net_cortex.mcp_server")

# Module-level state (initialized on server startup).
_task_store: TaskStore | None = None
_engine: Any = None  # NetCortexEngine instance, set at startup
_engine_cfg: dict[str, Any] | None = None

rca_server = MCPServer("NetCortex-RCA")


def initialize_server(engine: Any, cfg: dict[str, Any]) -> None:
    """Initialize the MCP server with engine and config. Called at startup."""
    global _task_store, _engine, _engine_cfg
    mcp_cfg = cfg.get("mcp_server", {})
    _task_store = TaskStore(
        ttl_seconds=int(mcp_cfg.get("task_ttl_seconds", 3600)),
        max_concurrent=int(mcp_cfg.get("max_concurrent_tasks", 10)),
    )
    _engine = engine
    _engine_cfg = cfg
    logger.info("MCP RCA Server initialized task_ttl=%s max_concurrent=%s",
                mcp_cfg.get("task_ttl_seconds", 3600),
                mcp_cfg.get("max_concurrent_tasks", 10))


@rca_server.tool()
async def run_rca(
    description: str,
    region: str,
    severity: str,
    scenario_id: int = 1,
    source_system: str = "mcp_client",
) -> dict:
    """Run root cause analysis on a network incident.

    This is a long-running operation. Returns a task handle immediately.
    Poll with the task_id to check progress and retrieve the final report.

    Args:
        description: Human-readable incident description.
        region: Affected network region (e.g., 'us-east').
        severity: Incident severity — low, medium, high, or critical.
        scenario_id: Simulation scenario ID (default: 1).
        source_system: Originating system identifier.
    """
    if _task_store is None or _engine is None:
        return {"error": "Server not initialized. Start with mcp-serve command."}

    # Validate severity
    if severity not in ("low", "medium", "high", "critical"):
        return {"error": f"Invalid severity: {severity}. Must be low|medium|high|critical."}

    # Create incident request
    incident = IncidentRequest(
        scenario_id=scenario_id,
        description=description,
        region=region,
        severity=severity,
        source_system=source_system,
        external_incident_id=f"MCP-{scenario_id}",
    )

    # Create task
    try:
        task_id = _task_store.create_task(incident.incident_id)
    except RuntimeError as e:
        return {"error": str(e)}

    logger.info("RCA task created task_id=%s incident_id=%s scenario=%s",
                task_id, incident.incident_id, scenario_id)

    # Launch the RCA pipeline in the background
    asyncio.create_task(_run_rca_background(task_id, incident))

    return {
        "task_id": task_id,
        "incident_id": incident.incident_id,
        "status": "running",
        "message": f"RCA analysis started. Poll with task_id='{task_id}' to check progress.",
    }


async def _run_rca_background(task_id: str, incident: IncidentRequest) -> None:
    """Background coroutine that runs the full RCA pipeline and updates task state."""
    try:
        # Progress: starting
        _task_store.update_progress(task_id, "supervisor", 10.0, "Classifying incident")

        # Run the full pipeline
        report: RCAReport = await _engine.run_incident(incident)

        # Progress: complete
        _task_store.update_progress(task_id, "complete", 100.0, "RCA analysis finished")

        # Serialize report
        report_data = report.model_dump(mode="json")

        # Include critical fields explicitly at top level for easy access
        result = {
            "incident_id": incident.incident_id,
            "root_cause": report.root_cause,
            "confidence_score": report.confidence_score,
            "conflict_detected": report.conflict_detected,
            "corroborating_domain_count": report.corroborating_domain_count,
            "human_readable_summary": report.human_readable_summary,
            "contributing_factors": report.contributing_factors,
            "causal_chain": report.causal_chain,
            "timed_out_agents": _get_timed_out_agents(report),
            "full_report": report_data,
        }

        _task_store.complete_task(task_id, result)
        logger.info("RCA task completed task_id=%s incident_id=%s confidence=%.2f conflict=%s",
                    task_id, incident.incident_id, report.confidence_score, report.conflict_detected)

    except Exception as e:
        error_msg = f"RCA pipeline failed: {type(e).__name__}: {e}"
        _task_store.fail_task(task_id, error_msg)
        logger.exception("RCA task failed task_id=%s incident_id=%s", task_id, incident.incident_id)


def _get_timed_out_agents(report: RCAReport) -> list[str]:
    """Extract timed-out agents from the engine state if available."""
    if _engine and _engine.last_result_state:
        return _engine.last_result_state.get("timed_out_agents", [])
    return []


@rca_server.tool()
async def get_report(incident_id: str) -> dict:
    """Retrieve a completed RCA report by incident_id or task_id.

    Args:
        incident_id: The incident ID or task ID to look up.
    """
    if _task_store is None:
        return {"error": "Server not initialized."}

    # Try as task_id first
    record = _task_store.get_task(incident_id)
    if record is None:
        # Try as incident_id
        record = _task_store.get_task_by_incident(incident_id)

    if record is None:
        return {"error": f"No task found for id: {incident_id}"}

    if record.status == TaskStatus.RUNNING:
        progress = None
        if record.progress:
            progress = {
                "stage": record.progress.stage,
                "percent": record.progress.percent,
                "message": record.progress.message,
            }
        return {
            "task_id": record.task_id,
            "incident_id": record.incident_id,
            "status": "running",
            "progress": progress,
            "message": "RCA analysis still in progress.",
        }

    if record.status == TaskStatus.COMPLETED:
        return {
            "task_id": record.task_id,
            "incident_id": record.incident_id,
            "status": "completed",
            "result": record.result,
        }

    if record.status == TaskStatus.FAILED:
        return {
            "task_id": record.task_id,
            "incident_id": record.incident_id,
            "status": "failed",
            "error": record.error,
        }

    if record.status == TaskStatus.CANCELLED:
        return {
            "task_id": record.task_id,
            "incident_id": record.incident_id,
            "status": "cancelled",
        }

    return {"error": "Unknown task status"}


@rca_server.tool()
async def list_scenarios() -> dict:
    """List all available simulation scenarios for RCA testing.

    Returns scenario IDs, names, and incident descriptions that can be
    passed to run_rca via the scenario_id parameter.
    """
    scenarios = []
    for sid, bundle in sorted(SCENARIOS.items()):
        scenarios.append({
            "id": sid,
            "name": bundle.scenario_name,
            "description": bundle.incident_request.description,
            "region": bundle.incident_request.region or "us-east",
            "severity": bundle.incident_request.severity,
            "expected_keywords": bundle.expected_rca_keywords,
        })
    return {"scenarios": scenarios, "count": len(scenarios)}
