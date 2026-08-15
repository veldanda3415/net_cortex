"""Runs every bundled scenario through the MCP server surface (run_rca ->
poll get_report) and checks the result is well-formed. Scenarios 10 and 12
get an explicit conflict_detected assertion, per the article's scope note
that a Task-shaped result is the easiest place to accidentally drop that
flag (e.g. hoisting the wrong nested field, or reading from a stale copy of
the report instead of the one that was just synthesized).
"""

from __future__ import annotations

import asyncio

import pytest

from simulation.scenarios import SCENARIOS

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS.keys()))
async def test_scenario_runs_end_to_end_via_mcp(mcp_server_module, tool_fn, poll_until_done, scenario_id):
    run_rca = tool_fn(mcp_server_module.run_rca)
    bundle = SCENARIOS[scenario_id]

    started = await run_rca(
        description=bundle.incident_request.description,
        region=bundle.incident_request.region or "us-east",
        severity=bundle.incident_request.severity,
        scenario_id=scenario_id,
    )
    assert started["status"] == "running", f"scenario {scenario_id}: run_rca did not accept task"

    final = await poll_until_done(mcp_server_module, started["task_id"], timeout=45.0)
    assert final["status"] == "completed", f"scenario {scenario_id}: task did not complete: {final}"

    result = final["result"]
    assert 0.0 <= result["confidence_score"] <= 1.0
    assert isinstance(result["conflict_detected"], bool)
    assert isinstance(result["corroborating_domain_count"], int)
    assert result["root_cause"]  # non-empty


async def test_scenario_10_conflict_detected_survives_task_result(mcp_server_module, tool_fn, poll_until_done):
    """Scenario 10: metrics anomaly on Switch-C, no corroborating config
    change. Expected to set conflict_detected=True at the report level."""
    run_rca = tool_fn(mcp_server_module.run_rca)
    bundle = SCENARIOS[10]

    started = await run_rca(
        description=bundle.incident_request.description,
        region=bundle.incident_request.region or "us-east",
        severity=bundle.incident_request.severity,
        scenario_id=10,
    )
    final = await poll_until_done(mcp_server_module, started["task_id"])

    assert final["status"] == "completed"
    top_level_flag = final["result"]["conflict_detected"]
    nested_flag = final["result"]["full_report"]["conflict_detected"]
    assert top_level_flag == nested_flag, (
        "conflict_detected diverged between the hoisted top-level field and "
        "full_report — this is exactly the drop the article scope calls out"
    )
    assert top_level_flag is True, (
        f"expected conflict_detected=True for scenario 10, got {top_level_flag}. "
        f"root_cause={final['result']['root_cause']!r}"
    )


async def test_scenario_12_conflict_detected_survives_task_result(mcp_server_module, tool_fn, poll_until_done):
    """Scenario 12: log+routing anomalies with mostly clean metrics —
    explicit cross-domain conflict fixture."""
    run_rca = tool_fn(mcp_server_module.run_rca)
    bundle = SCENARIOS[12]

    started = await run_rca(
        description=bundle.incident_request.description,
        region=bundle.incident_request.region or "us-east",
        severity=bundle.incident_request.severity,
        scenario_id=12,
    )
    final = await poll_until_done(mcp_server_module, started["task_id"])

    assert final["status"] == "completed"
    top_level_flag = final["result"]["conflict_detected"]
    nested_flag = final["result"]["full_report"]["conflict_detected"]
    assert top_level_flag == nested_flag
    assert top_level_flag is True, (
        f"expected conflict_detected=True for scenario 12, got {top_level_flag}. "
        f"root_cause={final['result']['root_cause']!r}"
    )


async def test_scenario_10_and_12_run_concurrently_without_cross_talk(mcp_server_module, tool_fn, poll_until_done):
    """Same intent as test_concurrent_runs_preserve_own_timed_out_agents but
    focused on conflict_detected specifically, since that's the field the
    article scope flags as highest-risk under the Task-shaped result path."""
    run_rca = tool_fn(mcp_server_module.run_rca)
    b10, b12 = SCENARIOS[10], SCENARIOS[12]

    started_10, started_12 = await asyncio.gather(
        run_rca(description=b10.incident_request.description, region="us-east", severity="high", scenario_id=10),
        run_rca(description=b12.incident_request.description, region="us-east", severity="high", scenario_id=12),
    )
    result_10, result_12 = await asyncio.gather(
        poll_until_done(mcp_server_module, started_10["task_id"]),
        poll_until_done(mcp_server_module, started_12["task_id"]),
    )

    assert result_10["result"]["conflict_detected"] is True
    assert result_12["result"]["conflict_detected"] is True
    assert result_10["result"]["incident_id"] == started_10["incident_id"]
    assert result_12["result"]["incident_id"] == started_12["incident_id"]
