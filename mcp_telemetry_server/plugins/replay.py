"""Replay source: serves recorded contract v2 fixtures, rebased into the
query window.

Used in CI today (deterministic data with realistic AWS shapes, no cloud
access) and later to replay recorded AWS sessions offline. A fixture is one
JSON file:

    {
      "contract_version": "2.0",
      "description": "TGW VPN tunnel down, recorded 2026-09-14",
      "region": "us-east-1",                     # optional; omitted = any region
      "recorded_window": {"start": "...", "end": "..."},
      "signals": [ <contract v2 Signal>, ... ],
      "topology": {"entities": [...], "relationships": [...]}   # optional
    }

Rebasing shifts every timestamp by ``query.end_time - recorded_window.end``,
so the incident's alarm instant lines up with the end of the recording and
relative spacing between signals is preserved exactly.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contract import Coverage, Entity, Signal, TelemetryQuery, TimeWindow, TopologyEdge, check_contract_version
from mcp_telemetry_server.plugins.base import SourcePlugin, register_plugin


class _RecordedTopology(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[TopologyEdge] = Field(default_factory=list)


class ReplayFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: str
    description: str = ""
    region: str | None = None
    recorded_window: TimeWindow
    signals: list[Signal] = Field(default_factory=list)
    topology: _RecordedTopology | None = None


def rebase_signal(signal: Signal, offset: timedelta, fixture_name: str) -> Signal:
    return signal.model_copy(
        update={
            "timestamp": signal.timestamp + offset,
            "observed_at": signal.observed_at + offset,
            "attributes": {**signal.attributes, "replay_fixture": fixture_name},
        }
    )


@register_plugin
class ReplayPlugin(SourcePlugin):
    name = "replay"
    domains = frozenset({"metrics", "logs", "routing", "config", "topology"})

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        paths: list[Path] = [Path(p) for p in self.config.get("fixtures", [])]
        fixtures_dir = self.config.get("fixtures_dir")
        if fixtures_dir:
            paths.extend(sorted(Path(fixtures_dir).glob("*.json")))
        if not paths:
            raise ValueError("ConfigValidationError: sources.replay needs `fixtures_dir` or `fixtures`")
        self._fixtures: list[tuple[str, ReplayFixture]] = []
        for path in paths:
            fixture = ReplayFixture.model_validate(json.loads(path.read_text(encoding="utf-8")))
            check_contract_version(fixture.contract_version)
            self._fixtures.append((path.name, fixture))

    def _matching(self, region: str) -> list[tuple[str, ReplayFixture]]:
        return [(n, f) for n, f in self._fixtures if f.region is None or f.region == region]

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        signals: list[Signal] = []
        for fixture_name, fixture in self._matching(query.region):
            offset = query.end_time - fixture.recorded_window.end
            for signal in fixture.signals:
                if signal.domain != domain:
                    continue
                rebased = rebase_signal(signal, offset, fixture_name)
                if query.start_time <= rebased.timestamp <= query.end_time:
                    signals.append(rebased)
        return signals, self.coverage(lag_seconds=0.0)

    async def topology(self, region: str, scenario_id: int | None) -> tuple[list[Entity], list[TopologyEdge], Coverage]:
        entities: list[Entity] = []
        edges: list[TopologyEdge] = []
        for _, fixture in self._matching(region):
            if fixture.topology is not None:
                entities.extend(fixture.topology.entities)
                edges.extend(fixture.topology.relationships)
        return entities, edges, self.coverage(lag_seconds=0.0)

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "fixtures": [name for name, _ in self._fixtures]}
