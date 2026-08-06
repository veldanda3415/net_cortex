"""Quick verification that the MCP Telemetry Server tools work in-process."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client
from mcp_telemetry_server.server import telemetry


async def main() -> None:
    async with Client(telemetry) as client:
        # List tools
        tools_result = await client.list_tools()
        tool_names = [t.name for t in tools_result.tools]
        print(f"Tools available: {tool_names}")
        assert len(tool_names) == 4, f"Expected 4 tools, got {len(tool_names)}"

        # Test get_metrics
        result = await client.call_tool("get_metrics", {"region": "us-east", "window_minutes": 30, "scenario_id": 1})
        data = json.loads(result.content[0].text)
        print(f"get_metrics: {data['count']} metrics for region={data['region']}")
        assert data["count"] > 0, "Expected metrics data"
        first = data["metrics"][0]
        assert "error_rate" in first, "Missing error_rate field"
        assert "packet_loss" in first, "Missing packet_loss field"
        assert "throughput_gbps" in first, "Missing throughput_gbps field"
        assert "latency_ms" in first, "Missing latency_ms field"
        assert "timestamp" in first, "Missing timestamp field"
        assert "region" in first, "Missing region field"
        print(f"  Sample: error_rate={first['error_rate']}, throughput={first['throughput_gbps']}Gbps")

        # Test get_logs
        result = await client.call_tool("get_logs", {"region": "us-east", "window_minutes": 30, "scenario_id": 1})
        data = json.loads(result.content[0].text)
        print(f"get_logs: {data['count']} log events")
        if data["count"] > 0:
            log = data["logs"][0]
            assert "level" in log, "Missing level field"
            assert "service" in log, "Missing service field"
            assert "message" in log, "Missing message field"
            print(f"  Sample: [{log['level']}] {log['service']}: {log['message'][:60]}")

        # Test get_routing_events
        result = await client.call_tool("get_routing_events", {"region": "us-east", "window_minutes": 30, "scenario_id": 1})
        data = json.loads(result.content[0].text)
        print(f"get_routing_events: {data['count']} events for region={data['region']}")

        # Test get_config_changes
        result = await client.call_tool("get_config_changes", {"region": "us-east", "window_minutes": 30, "scenario_id": 1})
        data = json.loads(result.content[0].text)
        print(f"get_config_changes: {data['count']} changes")
        if data["count"] > 0:
            change = data["config_changes"][0]
            assert "component" in change, "Missing component field"
            assert "change_type" in change, "Missing change_type field"
            print(f"  Sample: {change['component']} ({change['change_type']})")

        # Test with different scenario
        result = await client.call_tool("get_metrics", {"region": "us-east", "window_minutes": 30, "scenario_id": 7})
        data = json.loads(result.content[0].text)
        print(f"get_metrics (scenario 7): {data['count']} metrics")

        print("\n✓ ALL TELEMETRY SERVER TOOLS VERIFIED SUCCESSFULLY")


if __name__ == "__main__":
    asyncio.run(main())
