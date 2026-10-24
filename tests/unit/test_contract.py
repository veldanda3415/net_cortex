"""Contract v2: envelope validation, version checks, exported schema."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from contract import (
    ContractVersionError,
    Coverage,
    TelemetryQuery,
    TimeWindow,
    ToolResponse,
    check_contract_version,
)
from contract.export_schema import SCHEMA_DIR, render_schemas

END = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
WINDOW = TimeWindow(start=END - timedelta(minutes=30), end=END)


@pytest.mark.parametrize("region", ["us-east", "us-east-1", "ap-southeast-2", "us-gov-west-1"])
def test_query_accepts_datacenter_and_aws_region_names(region):
    TelemetryQuery(region=region, start_time=WINDOW.start, end_time=WINDOW.end)


@pytest.mark.parametrize("region", ["US East", "", "us_east_1"])
def test_query_rejects_malformed_regions(region):
    with pytest.raises(ValidationError):
        TelemetryQuery(region=region, start_time=WINDOW.start, end_time=WINDOW.end)


def test_query_rejects_inverted_window_and_normalizes_naive_times_to_utc():
    with pytest.raises(ValidationError):
        TelemetryQuery(region="us-east-1", start_time=END, end_time=END - timedelta(seconds=1))
    q = TelemetryQuery(region="us-east-1", start_time=datetime(2026, 9, 14, 9, 30), end_time=datetime(2026, 9, 14, 10, 0))
    assert q.end_time == END


def test_minor_version_accepted_major_rejected():
    check_contract_version("2.0")
    check_contract_version("2.7")  # minor bumps are additive
    with pytest.raises(ContractVersionError, match="major version 2"):
        check_contract_version("1.0")
    with pytest.raises(ContractVersionError, match="Malformed"):
        check_contract_version("two")


def test_no_data_versus_data_unavailable():
    clean = ToolResponse(window=WINDOW, coverage=[Coverage(source="a", status="ok")])
    assert clean.signals == [] and clean.status == "ok" and not clean.degraded  # verified no data

    unavailable = ToolResponse(window=WINDOW, coverage=[Coverage(source="a", status="failed", error="timeout")])
    assert unavailable.status == "failed" and unavailable.degraded

    no_coverage = ToolResponse(window=WINDOW)
    assert no_coverage.degraded  # nothing vouched for the window


def test_optional_source_failure_does_not_degrade():
    response = ToolResponse(window=WINDOW, coverage=[
        Coverage(source="a", status="ok"),
        Coverage(source="enrichment", status="failed", required=False),
    ])
    assert response.status == "partial"
    assert not response.degraded


def test_exported_json_schema_is_in_sync_with_models():
    for filename, content in render_schemas().items():
        assert (SCHEMA_DIR / filename).read_text(encoding="utf-8") == content, (
            f"{filename} is stale — run `python -m contract.export_schema`"
        )
