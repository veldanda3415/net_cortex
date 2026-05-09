from __future__ import annotations

from abc import ABC, abstractmethod

from models.schemas import ConfigChange, EntityBaseline, LogEvent, MetricSnapshot, RoutingEvent


class MetricsProvider(ABC):
    @abstractmethod
    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None) -> list[MetricSnapshot]:
        raise NotImplementedError


class LogProvider(ABC):
    @abstractmethod
    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None) -> list[LogEvent]:
        raise NotImplementedError


class RoutingProvider(ABC):
    @abstractmethod
    def get_routing_events(self, region: str, window_minutes: int, scenario_id: int | None) -> list[RoutingEvent]:
        raise NotImplementedError


class ConfigProvider(ABC):
    @abstractmethod
    def get_config_changes(self, region: str, window_minutes: int, scenario_id: int | None) -> list[ConfigChange]:
        raise NotImplementedError


class BaselineProvider(ABC):
    @abstractmethod
    def get_baseline(self, entity_key: str, metric: str) -> EntityBaseline | None:
        raise NotImplementedError
