from __future__ import annotations

from abc import ABC, abstractmethod

from models.schemas import ConfigChange, EntityBaseline, LogEvent, MetricSnapshot, RoutingEvent


class MetricsProvider(ABC):
    # True after a call whose fetch/parse failed and fell back to an empty
    # result, so callers can tell "verified no data" from "saw nothing
    # because the fetch failed". Simulation providers never set this
    # (there's nothing to fail); MCP/real adapters do. Read it immediately
    # after calling get_metrics(), before any `await`, since it reflects
    # only the most recent call on this (possibly shared/long-lived)
    # provider instance.
    degraded: bool = False

    @abstractmethod
    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None) -> list[MetricSnapshot]:
        raise NotImplementedError


class LogProvider(ABC):
    degraded: bool = False

    @abstractmethod
    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None) -> list[LogEvent]:
        raise NotImplementedError


class RoutingProvider(ABC):
    degraded: bool = False

    @abstractmethod
    def get_routing_events(self, region: str, window_minutes: int, scenario_id: int | None) -> list[RoutingEvent]:
        raise NotImplementedError


class ConfigProvider(ABC):
    degraded: bool = False

    @abstractmethod
    def get_config_changes(self, region: str, window_minutes: int, scenario_id: int | None) -> list[ConfigChange]:
        raise NotImplementedError


class BaselineProvider(ABC):
    @abstractmethod
    def get_baseline(self, entity_key: str, metric: str) -> EntityBaseline | None:
        raise NotImplementedError
