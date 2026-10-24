from __future__ import annotations

from abc import ABC, abstractmethod

from contract import TelemetryQuery, ToolResponse
from models.schemas import EntityBaseline


class TelemetryProvider(ABC):
    """Fetches one domain's telemetry for one query.

    Stateless per call: the returned ToolResponse carries its own coverage,
    and degradation is derived from that (`response.degraded`), never from
    provider instance state — a provider is shared by every concurrent RCA
    task in the process.
    """

    @abstractmethod
    async def fetch(self, domain: str, query: TelemetryQuery) -> ToolResponse:
        raise NotImplementedError


class BaselineProvider(ABC):
    @abstractmethod
    def get_baseline(self, entity_key: str, metric: str) -> EntityBaseline | None:
        raise NotImplementedError
