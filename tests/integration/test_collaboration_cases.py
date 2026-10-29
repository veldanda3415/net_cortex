"""Collaboration over topology, end to end through the real agents and engine,
on replay cases from evals/cases (built by evals/build_fixtures.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evals.runner import cases_config
from evals.scoring import load_expect, score_case
from models.schemas import IncidentRequest

CASES = Path(__file__).resolve().parents[2] / "evals" / "cases"


def _case(name: str) -> dict:
    return json.loads((CASES / f"{name}.json").read_text())


def _with_cases(cfg: dict) -> None:
    cases = [json.loads(p.read_text()) for p in sorted(CASES.glob("*.json"))]
    cfg.update(cases_config(cfg, cases, window_minutes=15))
    cfg.pop("providers", None)


@pytest.fixture
def cfg(cfg, request):
    """Override the conftest cfg with a telemetry section chosen per test."""
    configure = getattr(request, "param", None)
    if configure is not None:
        configure(cfg)
    return cfg


def _incident(case: dict) -> IncidentRequest:
    return IncidentRequest(scenario_id=case["scenario_id"], description=case["description"], region=case["region"],
                           severity="high", reported_at=datetime.now(timezone.utc),
                           alarm_entities=case["alarm_entities"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_cases], indirect=True)
async def test_peer_question_finds_a_cause_older_than_the_window(engine):
    case = _case("phz-record-deleted")
    report = await engine.run_incident(_incident(case))

    # Round 0: the record deletion is 19 minutes old, outside the 15-minute window.
    asked = [m for m in report.a2a_message_log if m.message_type == "validation_request"
             and m.payload["entity"]["type"] == "hosted_zone" and m.target_agent == "config"]
    assert asked and asked[0].sender_agent in {"log", "metrics"}
    assert asked[0].payload["response"]["verdict"] == "supported"
    assert any(m.message_type == "validation_response" and m.target_agent == asked[0].sender_agent
               for m in report.a2a_message_log)

    config = next(f for f in report.agent_findings if f.domain == "config")
    assert config.anomaly_detected and config.revised
    assert any(r.get("via") == "validation" and "ChangeResourceRecordSets" in r["signals"] for r in config.evidence)

    top = report.ranked_causes[0]
    assert (top.entity_type, top.entity_id, top.domain) == ("hosted_zone", "Z1030605VHK1NFO24118", "config")
    assert top.via_validation and top.features["topology"] == 1.0
    assert "Z1030605VHK1NFO24118" in report.root_cause
    assert score_case(case, report.model_dump(mode="json"), load_expect())["passed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_cases], indirect=True)
@pytest.mark.parametrize("name", ["tgw-propagation-removed", "asn-mismatch", "sg-rule-removed", "bad-route-map",
                                  "peering-route-removed", "loss-latency", "healthy"])
async def test_case_passes_without_llm(engine, name):
    case = _case(name)
    report = await engine.run_incident(_incident(case))
    result = score_case(case, report.model_dump(mode="json"), load_expect())
    assert result["passed"], result
