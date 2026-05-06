from __future__ import annotations

from providers.base import LogProvider


class SplunkAdapter(LogProvider):
    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None):
        raise NotImplementedError("Implement Splunk integration here")
