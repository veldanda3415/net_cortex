"""Record live telemetry as replay fixtures.

With `telemetry.record_dir` (or $NETCORTEX_RECORD_DIR) set, every response
the RCA side receives is merged into one fixture per alarm instant:
<record_dir>/<region>_<alarm>.json, in exactly the format the replay source
plugin serves (mcp_telemetry_server/plugins/replay.py). A live campaign then
leaves behind fixtures that reproduce each incident offline:

    sources:
      replay: {enabled: true, fixtures: [output/recordings/us-east-1_20261028T015951Z.json]}

Signals are deduplicated on (source, evidence_ref), the recorded window is the
union of every query window (baseline windows included), and the topology
seen for the region is stored alongside. Writes are synchronous read-merge-
write, so recorders of several agents in one process can share a file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from contract import CONTRACT_VERSION, Signal, TelemetryQuery, ToolResponse, TopologyResponse

logger = logging.getLogger("net_cortex.providers.recorder")


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class TelemetryRecorder:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._last_path: dict[str, Path] = {}

    def path_for(self, region: str, alarm_at: datetime) -> Path:
        return self.root / f"{region}_{alarm_at.strftime('%Y%m%dT%H%M%SZ')}.json"

    def _load(self, path: Path, region: str, alarm_at: datetime) -> dict[str, Any]:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {
            "contract_version": CONTRACT_VERSION,
            "description": f"Recorded by NetCortex for the alarm at {_iso(alarm_at)}",
            "region": region,
            "recorded_window": {"start": _iso(alarm_at), "end": _iso(alarm_at)},
            "alarm_at": _iso(alarm_at),
            "signals": [],
        }

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    def record(self, query: TelemetryQuery, response: ToolResponse) -> None:
        try:
            alarm_at = query.anchor_time or query.end_time
            path = self.path_for(query.region, alarm_at)
            data = self._load(path, query.region, alarm_at)
            window = data["recorded_window"]
            start = response.baseline_window.start if response.baseline_window else query.start_time
            window["start"] = min(window["start"], _iso(start))
            window["end"] = max(window["end"], _iso(query.end_time))
            seen = {(s.get("source"), s.get("evidence_ref") or json.dumps(s, sort_keys=True)) for s in data["signals"]}
            for signal in response.signals:
                row = signal.model_dump(mode="json")
                # Replay adds this on the way out; it is not part of the recording.
                row["attributes"].pop("replay_fixture", None)
                key = (row.get("source"), row.get("evidence_ref") or json.dumps(row, sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                data["signals"].append(row)
            self._write(path, data)
            self._last_path[query.region] = path
        except Exception:  # recording must never break an RCA
            logger.exception("Telemetry recording failed region=%s", query.region)

    def record_topology(self, topology: TopologyResponse) -> None:
        path = self._last_path.get(topology.region)
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["topology"] = {
                "entities": [e.model_dump(mode="json") for e in topology.entities],
                "relationships": [r.model_dump(mode="json") for r in topology.relationships],
            }
            self._write(path, data)
        except Exception:
            logger.exception("Topology recording failed region=%s", topology.region)


def load_signals(path: str | Path) -> list[Signal]:
    """The signals of a recorded fixture (handy in tests and notebooks)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Signal.model_validate(s) for s in data.get("signals", [])]
