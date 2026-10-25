"""Verify the RCA-side telemetry registry against the in-process telemetry server.

Exercises the same path the agents use (registry -> MCP endpoint client ->
telemetry MCP server -> simulation plugin -> catalog-driven detection) with
no network, plus a two-endpoint config where one endpoint is unreachable.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from contract.catalog_loader import get_catalog
from detection import analyze_domain
from providers.factory import create_telemetry_provider
from providers.query import query_from_request, query_window


async def main() -> None:
    start, end = query_window(datetime.now(timezone.utc), 30)
    window = {"start_time": start.isoformat(), "end_time": end.isoformat()}
    registry = create_telemetry_provider({})  # default: inproc://local simulation for every domain

    catalog = get_catalog()
    for domain_id, scenario in (("metrics", 1), ("log", 1), ("routing", 2), ("config", 1)):
        telemetry_domain = catalog.domain(domain_id).telemetry_domain
        query = query_from_request({"region": "us-east", "scenario_id": scenario, **window})
        response = await registry.fetch(telemetry_domain, query)
        finding = analyze_domain(catalog, domain_id, response, region="us-east")
        print(f"{domain_id}: {len(response.signals)} signal(s), coverage={response.status} "
              f"degraded={response.degraded} anomaly={finding.anomaly_detected}: {finding.summary[:70]}")
        assert response.status == "ok" and not response.degraded

    print("\n--- Two endpoints, one unreachable ---")
    cfg = {"telemetry": {"domains": {"metrics": {"endpoints": [
        {"name": "local", "url": "inproc://local"},
        {"name": "down", "url": "http://127.0.0.1:1/mcp", "timeout_seconds": 2, "retry": {"max_attempts": 1}},
    ]}}}}
    two = create_telemetry_provider(cfg, domains=("metrics",))
    response = await two.fetch("metrics", query_from_request({"region": "us-east", "scenario_id": 1, **window}))
    print(f"signals={len(response.signals)} status={response.status} degraded={response.degraded}")
    for c in response.coverage:
        print(f"  {c.source}: {c.status} {c.error or ''}")
    assert response.signals and response.status == "partial" and response.degraded

    await registry.aclose()
    await two.aclose()
    print("\n✓ TELEMETRY REGISTRY VERIFIED SUCCESSFULLY")


if __name__ == "__main__":
    asyncio.run(main())
