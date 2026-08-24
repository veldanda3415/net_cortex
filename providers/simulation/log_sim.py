from __future__ import annotations

from providers.base import LogProvider
from simulation.scenarios import SCENARIOS


class SimulationLogProvider(LogProvider):
    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None):
        scenario = SCENARIOS.get(scenario_id or 1)
        if scenario is None:
            raise ValueError(f"Unknown scenario_id: {scenario_id}")
        # LogEvent carries no per-row region field, so region filtering can
        # only happen at bundle granularity — every scenario is single-region
        # today, so this is equivalent to per-row filtering in practice.
        if scenario.incident_request.region != region:
            return []
        return scenario.log_events
