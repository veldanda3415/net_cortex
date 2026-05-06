from __future__ import annotations

from datetime import datetime, timezone

from models.schemas import IncidentRequest


def normalize_incident(payload: dict) -> IncidentRequest:
    return IncidentRequest(
        description=payload.get("description", "Unknown incident"),
        region=payload.get("region", "us-east"),
        severity=payload.get("severity", "medium"),
        scenario_id=payload.get("scenario_id"),
        source_system=payload.get("source_system"),
        external_incident_id=payload.get("external_incident_id"),
        reported_at=datetime.now(timezone.utc),
    )
