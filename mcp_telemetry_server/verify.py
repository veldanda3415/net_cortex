"""Quick verification that the MCP Telemetry Server tools work in-process."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client

from contract import ToolResponse, TopologyResponse
from mcp_telemetry_server.server import telemetry


async def main() -> None:
    end = datetime.now(timezone.utc)
    window = {"start_time": (end - timedelta(minutes=30)).isoformat(), "end_time": end.isoformat()}

    async with Client(telemetry) as client:
        tools_result = await client.list_tools()
        tool_names = [t.name for t in tools_result.tools]
        print(f"Tools available: {tool_names}")
        assert len(tool_names) == 5, f"Expected 5 tools, got {len(tool_names)}"

        for tool, scenario in (("get_metrics", 1), ("get_logs", 1), ("get_routing_events", 2),
                               ("get_config_changes", 1), ("get_metrics", 7)):
            result = await client.call_tool(tool, {"region": "us-east", "scenario_id": scenario, **window})
            response = ToolResponse.model_validate(json.loads(result.content[0].text))
            assert response.status == "ok", response.coverage
            print(f"{tool} (scenario {scenario}): {len(response.signals)} signal(s), coverage={response.status}")
            if response.signals:
                s = response.signals[0]
                print(f"  Sample: {s.signal_type} {s.name} {s.entity.type}={s.entity.id} value={s.value}")

        result = await client.call_tool("get_topology", {"region": "us-east-1"})
        topo = TopologyResponse.model_validate(json.loads(result.content[0].text))
        print(f"get_topology: {len(topo.entities)} entities, {len(topo.relationships)} relationships")

        result = await client.call_tool("get_metrics", {"region": "us-east", "contract_version": "1.0", **window})
        assert result.is_error, "major-version mismatch must be rejected"
        print("contract_version 1.0 rejected as expected")

        print("\n✓ ALL TELEMETRY SERVER TOOLS VERIFIED SUCCESSFULLY")


if __name__ == "__main__":
    asyncio.run(main())
