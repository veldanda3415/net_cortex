from __future__ import annotations

from models.schemas import EntityBaseline
from providers.base import BaselineProvider


class PrometheusBaselineProvider(BaselineProvider):
    def get_baseline(self, entity_key: str, metric: str) -> EntityBaseline | None:
        raise NotImplementedError("Implement Prometheus baseline query integration here")
