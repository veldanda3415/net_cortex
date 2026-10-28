from __future__ import annotations

from datetime import datetime, timezone

from ingestion.cloudwatch_alarms import incident_payload, is_alarm_event
from models.schemas import IncidentRequest

# Keys an upstream alarm may use for "when this was detected". reported_at
# anchors the telemetry query window, so a caller-supplied alarm time must
# win over ingest time — otherwise a delayed webhook queries the wrong window.
_ALARM_TIME_KEYS = ("reported_at", "alarm_time", "StateChangeTime")


def _alarm_time(payload: dict) -> datetime:
    for key in _ALARM_TIME_KEYS:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, datetime):
            value = raw
        else:
            # CloudWatch's StateChangeTime uses "+0000"; fromisoformat handles
            # that and "Z" on 3.11+. Let malformed values raise ValueError.
            value = datetime.fromisoformat(str(raw))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_incident(payload: dict) -> IncidentRequest:
    # A raw EventBridge alarm event (e.g. from an API destination) is mapped
    # by its alerting system's adapter first.
    if is_alarm_event(payload):
        payload = incident_payload([payload])
    return IncidentRequest(
        description=payload.get("description", "Unknown incident"),
        region=payload.get("region", "us-east"),
        severity=payload.get("severity", "medium"),
        scenario_id=payload.get("scenario_id"),
        source_system=payload.get("source_system"),
        external_incident_id=payload.get("external_incident_id"),
        reported_at=_alarm_time(payload),
    )
