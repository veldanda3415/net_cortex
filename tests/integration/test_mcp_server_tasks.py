"""Task lifecycle tests for mcp_server/server.py: run_rca / get_report /
list_scenarios, plus the concurrency-safety regression test for
timed_out_agents described in the comment at its call site in server.py.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def test_run_rca_returns_task_handle_immediately(mcp_server_module, tool_fn):
    run_rca = tool_fn(mcp_server_module.run_rca)
    result = await run_rca(
        description="High error rate and throughput drop in us-east region",
        region="us-east",
        severity="high",
        scenario_id=1,
    )
    assert result["status"] == "running"
    assert result["task_id"].startswith("task_")
    assert "incident_id" in result


async def test_run_rca_rejects_invalid_severity(mcp_server_module, tool_fn):
    run_rca = tool_fn(mcp_server_module.run_rca)
    result = await run_rca(description="x", region="us-east", severity="urgent", scenario_id=1)
    assert "error" in result
    assert "severity" in result["error"].lower()


async def test_get_report_reports_progress_while_running(mcp_server_module, tool_fn, poll_until_done):
    run_rca = tool_fn(mcp_server_module.run_rca)
    get_report = tool_fn(mcp_server_module.get_report)

    started = await run_rca(description="x", region="us-east", severity="high", scenario_id=1)
    # Immediately poll — pipeline is almost certainly still running.
    first = await get_report(started["task_id"])
    assert first["status"] in ("running", "completed")  # scenario 1 is fast; don't flake on timing
    if first["status"] == "running":
        assert "progress" in first

    final = await poll_until_done(mcp_server_module, started["task_id"])
    assert final["status"] == "completed"


async def test_get_report_hoists_critical_fields_on_completion(mcp_server_module, tool_fn, poll_until_done):
    run_rca = tool_fn(mcp_server_module.run_rca)
    started = await run_rca(
        description="Gateway PLR baseline shift", region="us-east", severity="high", scenario_id=1,
    )
    final = await poll_until_done(mcp_server_module, started["task_id"])

    assert final["status"] == "completed"
    result = final["result"]
    for field in (
        "confidence_score", "conflict_detected", "corroborating_domain_count",
        "root_cause", "human_readable_summary", "contributing_factors",
        "causal_chain", "timed_out_agents", "full_report",
    ):
        assert field in result, f"missing hoisted field: {field}"
    # full_report is the complete RCAReport — confidence should match the top-level copy.
    assert result["full_report"]["confidence_score"] == result["confidence_score"]


async def test_get_report_by_incident_id_matches_lookup_by_task_id(mcp_server_module, tool_fn, poll_until_done):
    run_rca = tool_fn(mcp_server_module.run_rca)
    get_report = tool_fn(mcp_server_module.get_report)

    started = await run_rca(description="x", region="us-east", severity="low", scenario_id=1)
    await poll_until_done(mcp_server_module, started["task_id"])

    by_task = await get_report(started["task_id"])
    by_incident = await get_report(started["incident_id"])
    assert by_task["result"]["root_cause"] == by_incident["result"]["root_cause"]


async def test_get_report_unknown_id_returns_error(mcp_server_module, tool_fn):
    get_report = tool_fn(mcp_server_module.get_report)
    result = await get_report("task_does_not_exist")
    assert "error" in result


async def test_list_scenarios_returns_all_fourteen(mcp_server_module, tool_fn):
    list_scenarios = tool_fn(mcp_server_module.list_scenarios)
    result = await list_scenarios()
    assert result["count"] == 14
    ids = {s["id"] for s in result["scenarios"]}
    assert ids == set(range(1, 15))


async def test_max_concurrent_tasks_enforced(mcp_server_module, tool_fn, poll_until_done):
    run_rca = tool_fn(mcp_server_module.run_rca)
    mcp_server_module._task_store._max_concurrent = 1  # tighten limit for this test

    first = await run_rca(description="x", region="us-east", severity="high", scenario_id=1)
    assert first["status"] == "running"

    second = await run_rca(description="y", region="us-east", severity="high", scenario_id=2)
    assert "error" in second
    assert "concurrent" in second["error"].lower()

    await poll_until_done(mcp_server_module, first["task_id"])


async def test_concurrent_runs_preserve_own_timed_out_agents(mcp_server_module, tool_fn, poll_until_done):
    """Regression test for the shared-instance-attribute hazard documented at
    the `_capture_timed_out_agents()` call site in mcp_server/server.py.

    Runs two different scenarios concurrently through the same engine and
    asserts each task's completed result reflects its own agent set, not
    whichever task happened to finish last. Scenario 10 and 12 don't
    naturally produce timed_out_agents (nothing times out in-simulation),
    so this test's primary job is to prove no cross-task field bleeds
    across into the wrong result — checked via root_cause, which does
    differ between scenarios and would catch any cross-task mixup in the
    surrounding result dict, not just the timed_out_agents field.
    """
    run_rca = tool_fn(mcp_server_module.run_rca)

    started_a, started_b = await asyncio.gather(
        run_rca(description="scenario 10 conflict probe", region="us-east", severity="high", scenario_id=10),
        run_rca(description="scenario 12 conflict probe", region="us-east", severity="high", scenario_id=12),
    )

    result_a, result_b = await asyncio.gather(
        poll_until_done(mcp_server_module, started_a["task_id"]),
        poll_until_done(mcp_server_module, started_b["task_id"]),
    )

    assert result_a["status"] == "completed"
    assert result_b["status"] == "completed"
    assert result_a["result"]["incident_id"] == started_a["incident_id"]
    assert result_b["result"]["incident_id"] == started_b["incident_id"]
    # The two scenarios have distinct expected root causes; if the shared
    # last_result_state attribute were read late (post-refactor regression),
    # this is the kind of field that would show cross-contamination first.
    assert result_a["result"]["root_cause"] != result_b["result"]["root_cause"]
    assert isinstance(result_a["result"]["timed_out_agents"], list)
    assert isinstance(result_b["result"]["timed_out_agents"], list)
