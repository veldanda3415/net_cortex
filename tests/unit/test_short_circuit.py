from __future__ import annotations

from datetime import datetime, timezone

from models.schemas import AgentFinding
from core.orchestrator import NetCortexEngine


def test_short_circuit_true_when_all_no_anomaly():
    findings = [
        AgentFinding(
            agent_id="metrics",
            domain="metrics",
            anomaly_detected=False,
            summary="noise",
            key_events=[],
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            confidence=0.2,
        ),
        AgentFinding(
            agent_id="log",
            domain="logs",
            anomaly_detected=False,
            summary="noise",
            key_events=[],
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            confidence=0.2,
        ),
    ]
    assert NetCortexEngine.should_short_circuit(findings)
