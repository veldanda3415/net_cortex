"""Quick verification that the MCP RCA Server tools work in-process.

This test:
1. Starts domain agents
2. Initializes the engine
3. Tests list_scenarios, run_rca, and get_report tools via in-process MCP client
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client
from mcp_server.server import initialize_server, rca_server


async def main() -> None:
    # Import runtime dependencies
    from app.main import load_config, start_runtime

    print("Loading config and starting runtime...")
    cfg = load_config("config/config.yaml")
    cfg.setdefault("llm", {})["require_success"] = False

    tasks, engine = await start_runtime(cfg)
    initialize_server(engine, cfg)

    print("Connecting to MCP RCA Server in-process...")
    async with Client(rca_server) as client:
        # Test 1: list_scenarios
        print("\n--- Test 1: list_scenarios ---")
        result = await client.call_tool("list_scenarios", {})
        data = json.loads(result.content[0].text)
        print(f"Scenarios available: {data['count']}")
        for s in data["scenarios"][:3]:
            print(f"  [{s['id']}] {s['name']}: {s['description'][:60]}")
        assert data["count"] >= 14, f"Expected >= 14 scenarios, got {data['count']}"

        # Test 2: run_rca (triggers async task)
        print("\n--- Test 2: run_rca ---")
        result = await client.call_tool("run_rca", {
            "description": "High error rate and throughput drop in us-east region",
            "region": "us-east",
            "severity": "high",
            "scenario_id": 1,
        })
        data = json.loads(result.content[0].text)
        print(f"Task created: {data['task_id']}")
        print(f"Incident ID: {data['incident_id']}")
        print(f"Status: {data['status']}")
        assert data["status"] == "running"
        task_id = data["task_id"]
        incident_id = data["incident_id"]

        # Test 3: Poll for completion
        print("\n--- Test 3: Polling task ---")
        max_wait = 60
        start = time.time()
        while time.time() - start < max_wait:
            await asyncio.sleep(2)
            result = await client.call_tool("get_report", {"incident_id": task_id})
            data = json.loads(result.content[0].text)
            if data["status"] == "completed":
                print(f"Task completed in {time.time() - start:.1f}s")
                break
            elif data["status"] == "failed":
                print(f"Task FAILED: {data.get('error')}")
                break
            else:
                progress = data.get("progress", {})
                print(f"  Polling... status={data['status']} stage={progress.get('stage', '?')}")
        else:
            print("TIMEOUT waiting for task completion!")
            # Cancel running tasks
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return

        # Test 4: Verify report content
        print("\n--- Test 4: Verify report ---")
        report = data["result"]
        print(f"Root cause: {report['root_cause'][:80]}")
        print(f"Confidence: {report['confidence_score']:.0%}")
        print(f"Conflict detected: {report['conflict_detected']}")
        print(f"Timed-out agents: {report['timed_out_agents']}")
        print(f"Contributing factors: {len(report['contributing_factors'])}")
        assert "confidence_score" in report
        assert "conflict_detected" in report
        assert "timed_out_agents" in report
        assert "full_report" in report

        # Test 5: get_report by incident_id
        print("\n--- Test 5: get_report by incident_id ---")
        result = await client.call_tool("get_report", {"incident_id": incident_id})
        data = json.loads(result.content[0].text)
        assert data["status"] == "completed"
        print(f"Report retrieved by incident_id: OK")

        print("\n✓ ALL MCP RCA SERVER TOOLS VERIFIED SUCCESSFULLY")

    # Cleanup
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
