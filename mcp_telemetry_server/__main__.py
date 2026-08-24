"""Run the MCP Telemetry Server standalone.

Usage:
    python -m mcp_telemetry_server
    python -m mcp_telemetry_server --port 9001
    python -m mcp_telemetry_server --transport stdio
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on path for simulation imports.
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp_telemetry_server.server import telemetry


def main() -> None:
    parser = argparse.ArgumentParser(description="NetCortex MCP Telemetry Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9001, help="Port to listen on (default: 9001)")
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "stdio"],
        default="streamable-http",
        help="MCP transport (default: streamable-http)",
    )
    args = parser.parse_args()

    print(f"Starting NetCortex MCP Telemetry Server on {args.host}:{args.port} ({args.transport})")
    telemetry.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
