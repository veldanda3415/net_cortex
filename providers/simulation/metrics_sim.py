from __future__ import annotations

from providers.base import MetricsProvider
from simulation.scenarios import SCENARIOS


class SimulationMetricsProvider(MetricsProvider):
    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None):
        scenario = SCENARIOS.get(scenario_id or 1)
        if scenario is None:
            raise ValueError(f"Unknown scenario_id: {scenario_id}")
        return [m for m in scenario.metrics_data if m.region == region]
