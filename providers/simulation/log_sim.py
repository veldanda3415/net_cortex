from __future__ import annotations

from providers.base import LogProvider
from simulation.scenarios import SCENARIOS


class SimulationLogProvider(LogProvider):
    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None):
        scenario = SCENARIOS.get(scenario_id or 1)
        return scenario.log_events
