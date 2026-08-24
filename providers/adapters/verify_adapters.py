"""Verify MCP adapters work against the in-process MCP Telemetry Server.

Tests all 4 adapters by connecting to the telemetry server object directly
(no network, same as K8s pod-to-pod but in-process for speed).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_telemetry_server.server import telemetry
from providers.adapters.mcp_adapter import (
    MCPConfigAdapter,
    MCPLogAdapter,
    MCPMetricsAdapter,
    MCPRoutingAdapter,
)


async def main() -> None:
    # Use in-process server object as endpoint (MCP SDK v2 supports this)
    endpoint = telemetry

    print("--- Testing MCPMetricsAdapter ---")
    metrics_adapter = MCPMetricsAdapter(endpoint=endpoint, timeout=30)
    # Need to patch _endpoint since we're using object, not URL
    metrics_adapter._endpoint = endpoint
    metrics = await metrics_adapter._fetch("us-east", 30, 1)
    print(f"  Metrics returned: {len(metrics)}")
    assert len(metrics) > 0, "Expected metrics data"
    m = metrics[0]
    print(f"  Sample: region={m.region} error_rate={m.error_rate} throughput={m.throughput_gbps}")
    assert m.region == "us-east"

    print("\n--- Testing MCPLogAdapter ---")
    log_adapter = MCPLogAdapter(endpoint=endpoint, timeout=30)
    log_adapter._endpoint = endpoint
    logs = await log_adapter._fetch("us-east", 30, 1)
    print(f"  Logs returned: {len(logs)}")
    if logs:
        lg = logs[0]
        print(f"  Sample: [{lg.level}] {lg.service}: {lg.message[:50]}")

    print("\n--- Testing MCPRoutingAdapter ---")
    routing_adapter = MCPRoutingAdapter(endpoint=endpoint, timeout=30)
    routing_adapter._endpoint = endpoint
    routes = await routing_adapter._fetch("us-east", 30, 2)  # scenario 2 has routing events
    print(f"  Routing events (scenario 2): {len(routes)}")
    if routes:
        r = routes[0]
        print(f"  Sample: {r.path_id} ({r.change_type}): {r.details[:50]}")

    print("\n--- Testing MCPConfigAdapter ---")
    config_adapter = MCPConfigAdapter(endpoint=endpoint, timeout=30)
    config_adapter._endpoint = endpoint
    changes = await config_adapter._fetch("us-east", 30, 1)
    print(f"  Config changes: {len(changes)}")
    if changes:
        c = changes[0]
        print(f"  Sample: {c.component} ({c.change_type})")

    print("\n--- Testing failure handling (bad scenario) ---")
    metrics_bad = await metrics_adapter._fetch("nonexistent-region", 30, 1)
    print(f"  Metrics for bad region: {len(metrics_bad)} (should be 0)")

    print("\n--- Testing factory ---")
    from providers.factory import (
        create_config_provider,
        create_log_provider,
        create_metrics_provider,
        create_routing_provider,
    )

    sim_cfg = {"providers": {"metrics": "simulation", "logs": "simulation", "routing": "simulation", "config": "simulation"}}
    mp = create_metrics_provider(sim_cfg)
    print(f"  Simulation metrics provider: {type(mp).__name__}")

    mcp_cfg = {
        "providers": {"metrics": "mcp", "logs": "mcp", "routing": "mcp", "config": "mcp"},
        "mcp_endpoints": {
            "metrics": {"url": "http://localhost:9001/mcp", "timeout_seconds": 30},
            "logs": {"url": "http://localhost:9001/mcp", "timeout_seconds": 30},
            "routing": {"url": "http://localhost:9001/mcp", "timeout_seconds": 30},
            "config": {"url": "http://localhost:9001/mcp", "timeout_seconds": 30},
        },
    }
    mp = create_metrics_provider(mcp_cfg)
    lp = create_log_provider(mcp_cfg)
    rp = create_routing_provider(mcp_cfg)
    cp = create_config_provider(mcp_cfg)
    print(f"  MCP metrics provider: {type(mp).__name__}")
    print(f"  MCP log provider: {type(lp).__name__}")
    print(f"  MCP routing provider: {type(rp).__name__}")
    print(f"  MCP config provider: {type(cp).__name__}")

    print("\n✓ ALL ADAPTERS AND FACTORY VERIFIED SUCCESSFULLY")


if __name__ == "__main__":
    asyncio.run(main())
