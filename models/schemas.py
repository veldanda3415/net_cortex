from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class IncidentRequest(BaseModel):
    incident_id: str = Field(default_factory=lambda: str(uuid4()))
    scenario_id: int | None = None
    description: str
    region: str
    reported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    severity: Literal["low", "medium", "high", "critical"]
    source_system: str | None = None
    external_incident_id: str | None = None


class MetricSnapshot(BaseModel):
    timestamp: datetime
    region: str
    error_rate: float
    packet_loss: float
    throughput_gbps: float
    latency_ms: float
    tags: dict[str, str] = Field(default_factory=dict)


class LogEvent(BaseModel):
    timestamp: datetime
    level: Literal["INFO", "WARN", "ERROR", "FATAL"]
    service: str
    message: str


class RoutingEvent(BaseModel):
    timestamp: datetime
    region: str
    path_id: str
    change_type: Literal["reroute", "flap", "congestion", "bgp_update"]
    details: str


class ConfigChange(BaseModel):
    timestamp: datetime
    component: str
    change_type: Literal["policy_update", "deployment", "bandwidth_limit", "rollback"]
    before: dict
    after: dict


class AgentFinding(BaseModel):
    agent_id: str
    domain: Literal["metrics", "log", "routing", "config"]
    anomaly_detected: bool
    summary: str
    key_events: list[dict]
    start_time: datetime
    end_time: datetime
    confidence: float
    revised: bool = False
    revision_count: int = 0


class A2AMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    sender_agent: str
    target_agent: Literal["broadcast"] | str
    message_type: Literal[
        "finding_publish",
        "clarification_request",
        "clarification_response",
        "validation_request",
        "validation_response",
        "finding_update",
    ]
    payload: dict
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    round_number: int


class RCAReport(BaseModel):
    incident_id: str
    root_cause: str
    contributing_factors: list[str]
    causal_chain: list[str]
    metrics_affected: list[str]
    human_readable_summary: str = Field(
        default="",
        description="Plain-English explanation written by LLM. Empty when running in simulation-only mode.",
    )
    agent_findings: list[AgentFinding]
    a2a_message_log: list[A2AMessage]
    confidence_score: float
    corroborating_domain_count: int
    conflict_detected: bool
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScenarioDataBundle(BaseModel):
    scenario_id: int
    scenario_name: str
    metrics_data: list[MetricSnapshot]
    log_events: list[LogEvent]
    routing_events: list[RoutingEvent]
    config_changes: list[ConfigChange]
    incident_request: IncidentRequest
    expected_rca_keywords: list[str]
