"""TEMPORARY v2 -> v1 translation so agent logic is unchanged in this release.

Converts contract v2 Signals back into the MetricSnapshot / LogEvent /
RoutingEvent / ConfigChange models the domain agents analyze today. Signals
that don't fit the old closed shapes (unknown metric names, change types
outside the old Literals, ...) are skipped here — they still travel in the
ToolResponse, the agents just can't see them until they read v2 signals
directly. Scheduled for removal once agents consume Signals natively.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import get_args

from pydantic import ValidationError

from contract import Signal, ToolResponse
from models.schemas import ConfigChange, LogEvent, MetricSnapshot, RoutingEvent

logger = logging.getLogger("net_cortex.providers.compat")

_METRIC_FIELDS = ("error_rate", "packet_loss", "throughput_gbps", "latency_ms")
_SEVERITY_TO_LEVEL = {"debug": "INFO", "info": "INFO", "warning": "WARN", "error": "ERROR", "critical": "FATAL"}
_ROUTING_CHANGE_TYPES = set(get_args(RoutingEvent.model_fields["change_type"].annotation))
_CONFIG_CHANGE_TYPES = set(get_args(ConfigChange.model_fields["change_type"].annotation))


def _region(signal: Signal, default: str) -> str:
    return str(signal.attributes.get("region") or signal.entity.labels.get("region") or default)


def to_metric_snapshots(response: ToolResponse, region: str) -> list[MetricSnapshot]:
    """Regroup per-metric signals into one MetricSnapshot per (time, entity).

    Groups missing any of the four legacy fields are dropped: filling them
    with a guess would fabricate an anomaly (e.g. throughput 0) or hide one.
    """
    groups: dict[tuple[datetime, str, tuple[tuple[str, str], ...]], dict] = {}
    for s in response.signals:
        if s.signal_type != "metric" or s.name not in _METRIC_FIELDS or s.value is None:
            continue
        key = (s.timestamp, _region(s, region), tuple(s.entity.labels.items()))
        group = groups.setdefault(key, {"timestamp": s.timestamp, "region": key[1], "tags": dict(s.entity.labels)})
        group[s.name] = s.value
    snapshots: list[MetricSnapshot] = []
    incomplete = 0
    for group in groups.values():
        if not all(name in group for name in _METRIC_FIELDS):
            incomplete += 1
            continue
        snapshots.append(MetricSnapshot(**group))
    if incomplete:
        logger.debug("compat: dropped %s metric group(s) missing legacy fields", incomplete)
    return snapshots


def to_log_events(response: ToolResponse) -> list[LogEvent]:
    events: list[LogEvent] = []
    for s in response.signals:
        if s.signal_type != "event":
            continue
        level = s.attributes.get("level") or _SEVERITY_TO_LEVEL[s.severity]
        message = s.attributes.get("message") or s.name
        try:
            events.append(LogEvent(timestamp=s.timestamp, level=level, service=s.entity.id, message=str(message)))
        except ValidationError:
            logger.debug("compat: skipped log signal %s", s.evidence_ref)
    return events


def to_routing_events(response: ToolResponse, region: str) -> list[RoutingEvent]:
    events: list[RoutingEvent] = []
    for s in response.signals:
        change_type = s.attributes.get("change_type")
        if s.signal_type != "event" or change_type not in _ROUTING_CHANGE_TYPES:
            continue
        events.append(RoutingEvent(
            timestamp=s.timestamp,
            region=_region(s, region),
            path_id=s.entity.id,
            change_type=change_type,
            details=str(s.attributes.get("details", s.name)),
        ))
    return events


def to_config_changes(response: ToolResponse) -> list[ConfigChange]:
    changes: list[ConfigChange] = []
    for s in response.signals:
        change_type = s.attributes.get("change_type")
        if s.signal_type != "event" or change_type not in _CONFIG_CHANGE_TYPES:
            continue
        before, after = s.attributes.get("before", {}), s.attributes.get("after", {})
        changes.append(ConfigChange(
            timestamp=s.timestamp,
            component=s.entity.id,
            change_type=change_type,
            before=before if isinstance(before, dict) else {"value": before},
            after=after if isinstance(after, dict) else {"value": after},
        ))
    return changes
