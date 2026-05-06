from __future__ import annotations

from providers.base import ConfigProvider, LogProvider, MetricsProvider, RoutingProvider


class MCPMetricsAdapter(MetricsProvider):
    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None):
        raise NotImplementedError("Implement MCP metrics tool integration here")


class MCPLogAdapter(LogProvider):
    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None):
        raise NotImplementedError("Implement MCP log tool integration here")


class MCPRoutingAdapter(RoutingProvider):
    def get_routing_events(self, region: str, window_minutes: int, scenario_id: int | None):
        raise NotImplementedError("Implement MCP routing tool integration here")


class MCPConfigAdapter(ConfigProvider):
    def get_config_changes(self, region: str, window_minutes: int, scenario_id: int | None):
        raise NotImplementedError("Implement MCP config tool integration here")
