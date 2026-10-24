"""Export the v2 contract as JSON Schema files under contract/schema/.

    python -m contract.export_schema           # write files
    python -m contract.export_schema --check   # exit 1 if files are stale (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from contract.models import CONTRACT_VERSION, Signal, TelemetryQuery, ToolResponse, TopologyResponse

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

EXPORTED: dict[str, type[BaseModel]] = {
    "signal": Signal,
    "telemetry_query": TelemetryQuery,
    "tool_response": ToolResponse,
    "topology_response": TopologyResponse,
}


def render_schemas() -> dict[str, str]:
    rendered: dict[str, str] = {}
    for name, model in EXPORTED.items():
        schema = model.model_json_schema()
        schema["$id"] = f"https://netcortex.dev/contract/{CONTRACT_VERSION}/{name}.json"
        schema["x-contract-version"] = CONTRACT_VERSION
        rendered[f"{name}.schema.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if exported files differ from the models")
    args = parser.parse_args()

    stale: list[str] = []
    SCHEMA_DIR.mkdir(exist_ok=True)
    for filename, content in render_schemas().items():
        path = SCHEMA_DIR / filename
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(filename)
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        print(f"Stale contract schema(s): {', '.join(stale)} — run `python -m contract.export_schema`")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
