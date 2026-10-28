"""CloudWatch alarm state-change events (EventBridge) -> /incidents payloads.

One adapter per alerting system keeps the RCA side vendor-neutral: an Azure
Monitor or GCP Cloud Monitoring alert would get a sibling module that maps its
own alert schema onto the same payload keys (description, region, severity,
source_system, external_incident_id, reported_at).

A single fault usually trips several alarms within a minute or two, so
`incident_payload` turns a burst of alarm events into one incident.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DETAIL_TYPE = "CloudWatch Alarm State Change"
MAX_REASON_CHARS = 200


def is_alarm_event(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("source") == "aws.cloudwatch" and payload.get("detail-type") == DETAIL_TYPE


def _state_time(event: dict[str, Any]) -> datetime:
    raw = (event.get("detail", {}).get("state", {}) or {}).get("timestamp") or event.get("time")
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_alarming(event: dict[str, Any]) -> bool:
    return (event.get("detail", {}).get("state", {}) or {}).get("value") == "ALARM"


def incident_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    """One /incidents payload for a burst of ALARM events, oldest first.

    reported_at is the latest alarm's state-change time, so the telemetry
    window ends after every alarm in the burst. The text says what fired
    (the symptom), never why.
    """
    if not events:
        raise ValueError("no alarm events")
    events = sorted(events, key=_state_time)
    seen: dict[str, str] = {}
    for event in events:
        detail = event.get("detail", {})
        name = detail.get("alarmName", "unknown-alarm")
        description = (detail.get("configuration", {}) or {}).get("description") or ""
        reason = (detail.get("state", {}) or {}).get("reason", "")[:MAX_REASON_CHARS]
        seen.setdefault(name, f"{name}: {description}".strip() if description else f"{name}: {reason}")
    first = events[0]
    return {
        "description": "CloudWatch alarms went into ALARM: " + "; ".join(seen.values()),
        "region": first.get("region", "us-east-1"),
        "severity": "high",
        "source_system": "cloudwatch",
        "external_incident_id": f"{first['detail'].get('alarmName', 'alarm')}@{_state_time(first).isoformat()}",
        "reported_at": _state_time(events[-1]).isoformat(),
        "alarms": list(seen),
    }
