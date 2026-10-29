"""Telemetry contract v2 — the wire shapes exchanged between the RCA pod and
telemetry MCP servers.

Deliberately open where the simulator used to be closed: ``Signal.name`` and
``Entity.type`` are free strings (a catalog validates them in a later
change), so a CloudWatch metric, a CloudTrail event and an FRR log line all
fit the same envelope without a schema change. See CONTRACT.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "2.1"
CONTRACT_MAJOR = 2

# Accepts both datacenter-style ("us-east") and AWS-style ("us-east-1",
# "ap-southeast-2", "us-gov-west-1") region names.
REGION_PATTERN = r"^[a-z]{2}(-[a-z0-9]+)+$"


class ContractVersionError(ValueError):
    """Raised when a peer speaks a different major contract version."""


def contract_major(version: str) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise ContractVersionError(f"Malformed contract_version {version!r}; expected '<major>.<minor>'") from exc


def check_contract_version(version: str) -> None:
    """Reject a version whose major differs from ours. Minor differences are
    additive by policy (new optional fields only) and are accepted."""
    major = contract_major(version)
    if major != CONTRACT_MAJOR:
        raise ContractVersionError(
            f"Unsupported contract_version {version!r}: this peer speaks major version "
            f"{CONTRACT_MAJOR} ({CONTRACT_VERSION}). Upgrade the older side; majors are not wire-compatible."
        )


def _as_utc(value: datetime) -> datetime:
    # Naive datetimes are treated as UTC rather than local time so a query
    # window means the same thing in every pod regardless of TZ settings.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(_Model):
    type: str = Field(description="Free-form entity type, e.g. 'tunnel', 'tgw_attachment', 'switch'.")
    id: str
    labels: dict[str, str] = Field(default_factory=dict)


class Signal(_Model):
    signal_type: Literal["metric", "event", "state"]
    timestamp: datetime = Field(description="Event time: when the thing happened at the source.")
    observed_at: datetime = Field(description="Ingest time: when the telemetry server read it.")
    entity: Entity
    name: str = Field(description="Free-form signal name, e.g. 'TunnelState', 'error_rate', 'ModifyVpnConnection'.")
    value: float | None = None
    unit: str | None = None
    severity: Literal["debug", "info", "warning", "error", "critical"] = "info"
    domain: str = Field(description="Telemetry domain: metrics | logs | routing | config | topology.")
    source: str = Field(description="Plugin that produced the signal, e.g. 'cloudwatch', 'simulation'.")
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_ref: str | None = Field(
        default=None,
        description="Raw pointer back to the source record (CloudTrail eventID, log group/stream + query id, ...). "
        "Together with `source` it is the dedupe key when several endpoints return the same record.",
    )

    @field_validator("timestamp", "observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class TimeWindow(_Model):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def _ordered(self) -> "TimeWindow":
        if self.start > self.end:
            raise ValueError("window start must be <= end")
        return self


class Coverage(_Model):
    source: str
    status: Literal["ok", "partial", "failed"]
    lag_seconds: float | None = Field(default=None, description="How far behind real time the source is, if known.")
    truncated: bool = Field(default=False, description="True when the source hit a page/row limit.")
    error: str | None = None
    required: bool = Field(
        default=True,
        description="Whether a non-ok status on this source makes the response degraded. "
        "Optional sources (enrichment) can fail without degrading the finding.",
    )


class EntityFilter(_Model):
    type: str | None = None
    id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    def matches(self, entity: Entity) -> bool:
        if self.type is not None and entity.type != self.type:
            return False
        if self.id is not None and entity.id != self.id:
            return False
        return all(entity.labels.get(k) == v for k, v in self.labels.items())


class TelemetryQuery(_Model):
    region: str = Field(pattern=REGION_PATTERN)
    start_time: datetime
    end_time: datetime
    entity_filters: list[EntityFilter] | None = None
    include_baseline: bool = False
    scenario_id: int | None = Field(default=None, description="Simulation only; ignored by every other source.")
    anchor_time: datetime | None = Field(
        default=None,
        description="The alarm instant this query belongs to (added in 2.1). Live sources ignore it; the replay "
        "source aligns a recording's alarm_at to it, so queries after the alarm line up with the recording.",
    )

    @field_validator("start_time", "end_time", "anchor_time")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)

    @model_validator(mode="after")
    def _ordered(self) -> "TelemetryQuery":
        if self.start_time > self.end_time:
            raise ValueError("start_time must be <= end_time")
        return self

    @property
    def window(self) -> TimeWindow:
        return TimeWindow(start=self.start_time, end=self.end_time)

    def matches(self, signal: Signal) -> bool:
        if not self.entity_filters:
            return True
        return any(f.matches(signal.entity) for f in self.entity_filters)


class ToolResponse(_Model):
    contract_version: str = CONTRACT_VERSION
    window: TimeWindow
    baseline_window: TimeWindow | None = None
    coverage: list[Coverage] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    next_cursor: str | None = None

    @property
    def degraded(self) -> bool:
        """True when some required data could not be read, so an empty or
        quiet result must not be read as a verified absence of anomalies."""
        if not self.coverage:
            return True
        return any(c.required and c.status != "ok" for c in self.coverage)

    @property
    def status(self) -> Literal["ok", "partial", "failed"]:
        statuses = {c.status for c in self.coverage}
        if not statuses or statuses == {"failed"}:
            return "failed"
        if statuses == {"ok"}:
            return "ok"
        return "partial"


class TopologyEdge(_Model):
    type: str = Field(description="Relationship type, e.g. 'attached_to', 'routes_via', 'peers_with'.")
    source: Entity
    target: Entity
    attributes: dict[str, Any] = Field(default_factory=dict)


class TopologyResponse(_Model):
    contract_version: str = CONTRACT_VERSION
    region: str
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[TopologyEdge] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
