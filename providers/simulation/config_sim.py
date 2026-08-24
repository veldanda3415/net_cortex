from __future__ import annotations

from providers.base import ConfigProvider
from simulation.scenarios import SCENARIOS


class SimulationConfigProvider(ConfigProvider):
    def get_config_changes(self, region: str, window_minutes: int, scenario_id: int | None):
        scenario = SCENARIOS.get(scenario_id or 1)
        if scenario is None:
            raise ValueError(f"Unknown scenario_id: {scenario_id}")
        # ConfigChange carries no per-row region field, so region filtering
        # can only happen at bundle granularity — see log_sim.py for the
        # same tradeoff.
        if scenario.incident_request.region != region:
            return []
        return scenario.config_changes
