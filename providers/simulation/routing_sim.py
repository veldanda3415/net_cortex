from __future__ import annotations

from providers.base import RoutingProvider
from simulation.scenarios import SCENARIOS


class SimulationRoutingProvider(RoutingProvider):
    def get_routing_events(self, region: str, window_minutes: int, scenario_id: int | None):
        scenario = SCENARIOS.get(scenario_id or 1)
        if scenario is None:
            raise ValueError(f"Unknown scenario_id: {scenario_id}")
        return [r for r in scenario.routing_events if r.region == region]
