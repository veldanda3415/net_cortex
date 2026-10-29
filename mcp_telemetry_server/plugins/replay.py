"""Replay source: serves recorded contract v2 fixtures, rebased into the
query window.

Used in CI today (deterministic data with realistic AWS shapes, no cloud
access) and later to replay recorded AWS sessions offline. A fixture is one
JSON file:

    {
      "contract_version": "2.0",
      "description": "TGW VPN tunnel down, recorded 2026-09-14",
      "region": "us-east-1",                     # optional; omitted = any region
      "scenario_id": 2001,                       # optional; set = only queries with this scenario_id
      "recorded_window": {"start": "...", "end": "..."},
      "alarm_at": "...",                         # optional; defaults to recorded_window.end
      "signals": [ <contract v2 Signal>, ... ],
      "topology": {"entities": [...], "relationships": [...]}   # optional
    }

Rebasing shifts every timestamp so the recording's alarm instant (``alarm_at``,
default ``recorded_window.end``) lands on the query's ``anchor_time`` (default
``end_time``). Relative spacing between signals is preserved exactly, and a
query for the minutes after the alarm (is it still happening?) gets the
recording's post-alarm signals.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contract import Coverage, Entity, Signal, TelemetryQuery, TimeWindow, TopologyEdge, check_contract_version
from mcp_telemetry_server.plugins.base import BASELINE_LOOKBACK, SourcePlugin, register_plugin


class _RecordedTopology(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[TopologyEdge] = Field(default_factory=list)


class ReplayFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: str
    description: str = ""
    region: str | None = None
    scenario_id: int | None = None
    recorded_window: TimeWindow
    alarm_at: datetime | None = None
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
    domains = frozenset({"*"})  # any catalog domain: fixtures say which domain each signal is

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

    def _matching(self, region: str, scenario_id: int | None) -> list[tuple[str, ReplayFixture]]:
        return [
            (n, f) for n, f in self._fixtures
            if (f.region is None or f.region == region) and (f.scenario_id is None or f.scenario_id == scenario_id)
        ]

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        signals: list[Signal] = []
        lower = query.start_time - BASELINE_LOOKBACK if query.include_baseline else query.start_time
        for fixture_name, fixture in self._matching(query.region, query.scenario_id):
            alarm_at = fixture.alarm_at or fixture.recorded_window.end
            offset = (query.anchor_time or query.end_time) - alarm_at
            for signal in fixture.signals:
                if signal.domain != domain:
                    continue
                rebased = rebase_signal(signal, offset, fixture_name)
                if lower <= rebased.timestamp <= query.end_time:
                    signals.append(rebased)
        return signals, self.coverage(lag_seconds=0.0)

    async def topology(self, region: str, scenario_id: int | None) -> tuple[list[Entity], list[TopologyEdge], Coverage]:
        entities: list[Entity] = []
        edges: list[TopologyEdge] = []
        for _, fixture in self._matching(region, scenario_id):
            if fixture.topology is not None:
                entities.extend(fixture.topology.entities)
                edges.extend(fixture.topology.relationships)
        return entities, edges, self.coverage(lag_seconds=0.0)

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "fixtures": [name for name, _ in self._fixtures]}
