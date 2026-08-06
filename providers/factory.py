"""Provider Factory — creates provider instances based on config mode.

Supports two modes per domain:
  - 'simulation': Use bundled SCENARIOS data (default, no network)
  - 'mcp': Fetch data from an external MCP Telemetry Server via MCP protocol

Config structure:
  providers:
    metrics: simulation  # or 'mcp'
    logs: simulation
    routing: simulation
    config: simulation

  mcp_endpoints:
    metrics:
      url: "http://netcortex-telemetry:9001/mcp"
      timeout_seconds: 30
    logs:
      url: "..."
      timeout_seconds: 30
    routing:
      url: "..."
      timeout_seconds: 30
    config:
      url: "..."
      timeout_seconds: 30
"""

from __future__ import annotations

import logging
from typing import Any

from providers.base import ConfigProvider, LogProvider, MetricsProvider, RoutingProvider

logger = logging.getLogger("net_cortex.providers.factory")


def create_metrics_provider(cfg: dict[str, Any]) -> MetricsProvider:
    """Create a metrics provider based on config."""
    mode = cfg.get("providers", {}).get("metrics", "simulation")

    if mode == "simulation":
        from providers.simulation.metrics_sim import SimulationMetricsProvider
        return SimulationMetricsProvider()

    elif mode == "mcp":
        from providers.adapters.mcp_adapter import MCPMetricsAdapter
        ep = cfg.get("mcp_endpoints", {}).get("metrics", {})
        url = ep.get("url", "")
        timeout = int(ep.get("timeout_seconds", 30))
        if not url:
            raise ValueError("mcp_endpoints.metrics.url must be set when providers.metrics='mcp'")
        logger.info("Creating MCPMetricsAdapter endpoint=%s timeout=%s", url, timeout)
        return MCPMetricsAdapter(endpoint=url, timeout=timeout)

    else:
        raise ValueError(f"Unknown metrics provider mode: '{mode}'. Must be 'simulation' or 'mcp'.")


def create_log_provider(cfg: dict[str, Any]) -> LogProvider:
    """Create a log provider based on config."""
    mode = cfg.get("providers", {}).get("logs", "simulation")

    if mode == "simulation":
        from providers.simulation.log_sim import SimulationLogProvider
        return SimulationLogProvider()

    elif mode == "mcp":
        from providers.adapters.mcp_adapter import MCPLogAdapter
        ep = cfg.get("mcp_endpoints", {}).get("logs", {})
        url = ep.get("url", "")
        timeout = int(ep.get("timeout_seconds", 30))
        if not url:
            raise ValueError("mcp_endpoints.logs.url must be set when providers.logs='mcp'")
        logger.info("Creating MCPLogAdapter endpoint=%s timeout=%s", url, timeout)
        return MCPLogAdapter(endpoint=url, timeout=timeout)

    else:
        raise ValueError(f"Unknown logs provider mode: '{mode}'. Must be 'simulation' or 'mcp'.")


def create_routing_provider(cfg: dict[str, Any]) -> RoutingProvider:
    """Create a routing provider based on config."""
    mode = cfg.get("providers", {}).get("routing", "simulation")

    if mode == "simulation":
        from providers.simulation.routing_sim import SimulationRoutingProvider
        return SimulationRoutingProvider()

    elif mode == "mcp":
        from providers.adapters.mcp_adapter import MCPRoutingAdapter
        ep = cfg.get("mcp_endpoints", {}).get("routing", {})
        url = ep.get("url", "")
        timeout = int(ep.get("timeout_seconds", 30))
        if not url:
            raise ValueError("mcp_endpoints.routing.url must be set when providers.routing='mcp'")
        logger.info("Creating MCPRoutingAdapter endpoint=%s timeout=%s", url, timeout)
        return MCPRoutingAdapter(endpoint=url, timeout=timeout)

    else:
        raise ValueError(f"Unknown routing provider mode: '{mode}'. Must be 'simulation' or 'mcp'.")


def create_config_provider(cfg: dict[str, Any]) -> ConfigProvider:
    """Create a config provider based on config."""
    mode = cfg.get("providers", {}).get("config", "simulation")

    if mode == "simulation":
        from providers.simulation.config_sim import SimulationConfigProvider
        return SimulationConfigProvider()

    elif mode == "mcp":
        from providers.adapters.mcp_adapter import MCPConfigAdapter
        ep = cfg.get("mcp_endpoints", {}).get("config", {})
        url = ep.get("url", "")
        timeout = int(ep.get("timeout_seconds", 30))
        if not url:
            raise ValueError("mcp_endpoints.config.url must be set when providers.config='mcp'")
        logger.info("Creating MCPConfigAdapter endpoint=%s timeout=%s", url, timeout)
        return MCPConfigAdapter(endpoint=url, timeout=timeout)

    else:
        raise ValueError(f"Unknown config provider mode: '{mode}'. Must be 'simulation' or 'mcp'.")
