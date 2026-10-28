"""Query-window helpers shared by the orchestrator and the domain agents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from contract import TelemetryQuery

DEFAULT_WINDOW_MINUTES = 30


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def query_window(reported_at: datetime, window_minutes: int) -> tuple[datetime, datetime]:
    """Anchor the window on the alarm: end = reported_at, start = end - window."""
    end = _utc(reported_at)
    return end - timedelta(minutes=window_minutes), end


def query_from_request(data: dict[str, Any]) -> TelemetryQuery:
    """Build a TelemetryQuery from an analyze-* A2A payload.

    The orchestrator always sends start_time/end_time; direct A2A callers that
    only send window_minutes get a window ending now.
    """
    window = int(data.get("window_minutes") or DEFAULT_WINDOW_MINUTES)
    end_raw = data.get("end_time")
    end = datetime.fromisoformat(end_raw) if isinstance(end_raw, str) else (end_raw or datetime.now(timezone.utc))
    anchor_raw = data.get("anchor_time")
    anchor = datetime.fromisoformat(anchor_raw) if isinstance(anchor_raw, str) else anchor_raw
    start_raw = data.get("start_time")
    start = datetime.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw
    if start is None:
        start, end = query_window(end, window)
    return TelemetryQuery(
        region=data["region"],
        start_time=start,
        end_time=end,
        entity_filters=data.get("entity_filters"),
        include_baseline=bool(data.get("include_baseline", False)),
        scenario_id=data.get("scenario_id"),
        anchor_time=anchor,
    )
