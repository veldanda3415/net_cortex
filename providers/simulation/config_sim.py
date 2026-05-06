from __future__ import annotations

from providers.base import ConfigProvider
from simulation.scenarios import SCENARIOS


class SimulationConfigProvider(ConfigProvider):
    def get_config_changes(self, region: str, window_minutes: int, scenario_id: int | None):
        scenario = SCENARIOS.get(scenario_id or 1)
        return scenario.config_changes
