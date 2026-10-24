"""In-process telemetry MCP servers addressed as `inproc://<name>`.

The RCA pod always talks MCP to telemetry, even for local simulation runs:
an `inproc://` endpoint is a real telemetry MCP server (same tools, same
contract, same plugins) hosted inside this process and reached through the
SDK's in-memory transport. Only plugins that need no cloud credentials may
run this way, so IAM never lands in the RCA pod.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

_SERVERS: dict[str, MCPServer] = {}

DEFAULT_INPROC_URL = "inproc://local"
DEFAULT_INPROC_SOURCES: dict[str, Any] = {"simulation": {"enabled": True}}


def _name(url: str) -> str:
    return url.removeprefix("inproc://").strip("/") or "local"


def register_inproc_server(url: str, server: MCPServer) -> None:
    _SERVERS[_name(url)] = server


def unregister_inproc_server(url: str) -> None:
    _SERVERS.pop(_name(url), None)


def ensure_inproc_server(url: str, sources: dict[str, Any] | None) -> MCPServer:
    name = _name(url)
    if name not in _SERVERS:
        from mcp_telemetry_server.server import build_server
        from mcp_telemetry_server.service import TelemetryService

        service = TelemetryService(sources or DEFAULT_INPROC_SOURCES, allow_cloud_sources=False)
        _SERVERS[name] = build_server(service, name=f"NetCortex-Telemetry-inproc-{name}")
    return _SERVERS[name]


def resolve_inproc_server(url: str) -> MCPServer:
    try:
        return _SERVERS[_name(url)]
    except KeyError:
        raise LookupError(f"no in-process telemetry server registered for {url}") from None
