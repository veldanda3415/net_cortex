from __future__ import annotations

from providers.base import MetricsProvider


class PrometheusAdapter(MetricsProvider):
    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None):
        raise NotImplementedError("Implement Prometheus query integration here")
