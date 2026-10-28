"""Source plugin interface and registry for the telemetry MCP server.

A source plugin reads one backend (CloudWatch, CloudTrail, a recorded
fixture, the simulator, ...) and emits contract v2 ``Signal`` objects plus a
``Coverage`` entry describing how complete that read was. Plugins register
themselves with ``@register_plugin`` and are enabled/configured under the
server's ``sources:`` config section — adding a backend never touches the
tool handlers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any, ClassVar

from contract import Coverage, Entity, Signal, TelemetryQuery, TopologyEdge

# How far before the window `include_baseline` reaches (TelemetryService uses the same).
BASELINE_LOOKBACK = timedelta(hours=24)

# "*" in a plugin's `domains` means "any domain the catalog defines" (e.g.
# replay can serve a domain added through the catalog alone).
ANY_DOMAIN = "*"

_PLUGINS: dict[str, type["SourcePlugin"]] = {}


class SourcePlugin(ABC):
    name: ClassVar[str]
    domains: ClassVar[frozenset[str]]
    # False for interface-only skeletons: enabling one fails server startup
    # instead of serving a guaranteed-failed coverage entry on every call.
    implemented: ClassVar[bool] = True
    # True for plugins that read cloud APIs. These are refused in the RCA
    # pod's in-process telemetry server so cloud credentials and IAM stay in
    # telemetry pods only.
    needs_cloud_credentials: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        configured = self.config.get("domains")
        if configured is not None:
            unknown = set(configured) - set(type(self).domains)
            if unknown and ANY_DOMAIN not in type(self).domains:
                raise ValueError(
                    f"ConfigValidationError: sources.{self.name}.domains has {sorted(unknown)}, "
                    f"but plugin '{self.name}' only serves {sorted(type(self).domains)}"
                )
            self.domains = frozenset(configured)

    @abstractmethod
    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        """Return signals for ``domain`` inside the query window, plus coverage.

        Raise on hard failure — the service turns exceptions into a
        ``failed`` coverage entry. Return ``status="partial"`` yourself when
        you got some but not all of the data (page limit, one log group
        unreadable, ...).
        """

    async def topology(self, region: str, scenario_id: int | None) -> tuple[list[Entity], list[TopologyEdge], Coverage]:
        raise NotImplementedError(f"source '{self.name}' does not serve topology")

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def serves(self, domain: str) -> bool:
        return domain in self.domains or ANY_DOMAIN in self.domains

    def coverage(self, status: str = "ok", **kwargs: Any) -> Coverage:
        return Coverage(source=self.name, status=status, required=bool(self.config.get("required", True)), **kwargs)


def register_plugin(cls: type[SourcePlugin]) -> type[SourcePlugin]:
    name = getattr(cls, "name", None)
    if not name:
        raise TypeError(f"{cls.__name__} must define a class-level `name`")
    if not cls.domains or not all(isinstance(d, str) and d for d in cls.domains):
        raise TypeError(f"plugin '{name}' must declare a non-empty set of domain names")
    existing = _PLUGINS.get(name)
    if existing is not None and existing is not cls:
        raise TypeError(f"plugin name '{name}' already registered by {existing.__name__}")
    _PLUGINS[name] = cls
    return cls


def get_plugin_class(name: str) -> type[SourcePlugin]:
    try:
        return _PLUGINS[name]
    except KeyError:
        raise ValueError(
            f"ConfigValidationError: unknown source plugin '{name}'. Registered: {', '.join(sorted(_PLUGINS))}"
        ) from None


def registered_plugins() -> dict[str, type[SourcePlugin]]:
    return dict(_PLUGINS)


class SkeletonPlugin(SourcePlugin):
    """Base for interface-only plugins whose backend integration lands later."""

    implemented = False
    needs_cloud_credentials = True

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        raise NotImplementedError(
            f"source plugin '{self.name}' is an interface skeleton; "
            "see project_docs/provider-adapter-guide.md for how to implement it"
        )

    def health(self) -> dict[str, Any]:
        return {"status": "not_implemented"}
