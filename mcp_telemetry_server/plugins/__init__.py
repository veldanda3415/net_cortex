"""Telemetry source plugins. Importing this package registers every bundled
plugin with the registry in plugins/base.py."""

from mcp_telemetry_server.plugins import aws, legacy_backends, replay, simulation  # noqa: F401  (registration side effects)
from mcp_telemetry_server.plugins.base import (
    SkeletonPlugin,
    SourcePlugin,
    get_plugin_class,
    register_plugin,
    registered_plugins,
)

__all__ = ["SkeletonPlugin", "SourcePlugin", "get_plugin_class", "register_plugin", "registered_plugins"]
