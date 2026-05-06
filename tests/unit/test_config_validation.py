from __future__ import annotations

import pytest

from core.orchestrator import NetCortexEngine, validate_config


def test_validate_config_success():
    cfg = {
        "a2a": {
            "max_iterations": 2,
            "analysis_timeout_seconds": 20,
            "message_timeout_seconds": 10,
            "round_timeout_seconds": 25,
            "collaboration_timeout_seconds": 60,
        }
    }
    validate_config(cfg)


def test_validate_config_failure():
    cfg = {
        "a2a": {
            "max_iterations": 2,
            "analysis_timeout_seconds": 80,
            "message_timeout_seconds": 30,
            "round_timeout_seconds": 20,
            "collaboration_timeout_seconds": 10,
        }
    }
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_short_circuit_logic_false_when_anomaly():
    from models.schemas import AgentFinding
    from datetime import datetime, timezone

    findings = [
        AgentFinding(
            agent_id="metrics",
            domain="metrics",
            anomaly_detected=True,
            summary="x",
            key_events=[],
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            confidence=0.8,
        )
    ]
    assert not NetCortexEngine.should_short_circuit(findings)
