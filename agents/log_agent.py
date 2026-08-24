from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI

from agents.a2a_protocol import build_task_result, extract_request_context
from models.schemas import AgentFinding
from providers.factory import create_log_provider


logger = logging.getLogger("net_cortex.agent.log")


def reconsider_finding(finding: AgentFinding, peer_findings: list[AgentFinding]) -> AgentFinding:
    """Adjust log confidence/summary using peer domain evidence."""
    revised = finding.model_copy(deep=True)
    peer_anomalies = [peer for peer in peer_findings if peer.anomaly_detected]

    if revised.anomaly_detected:
        # Idempotency guard: see metrics_agent.reconsider_finding for why this
        # must gate the whole block, not just the summary text append.
        if "Peer corroboration:" in revised.summary:
            return revised
        corroborating_domains = sorted({p.domain for p in peer_anomalies})
        if corroborating_domains:
            revised.revised = True
            revised.revision_count += 1
            revised.confidence = min(0.93, revised.confidence + 0.05)
            revised.summary = (
                f"{revised.summary}. "
                f"Peer corroboration: aligned anomalies from [{', '.join(corroborating_domains)}]"
            )
        return revised

    # No log anomaly while peers detect issues is useful contradiction evidence.
    if peer_anomalies and "Peer contradiction:" not in revised.summary:
        revised.revised = True
        revised.revision_count += 1
        revised.confidence = max(revised.confidence, 0.74)
        contradictory_domains = sorted({p.domain for p in peer_anomalies})
        revised.summary = (
            f"{revised.summary}. "
            f"Peer contradiction: no direct log error pattern while [{', '.join(contradictory_domains)}] reported anomalies"
        )

    return revised


def build_log_app(cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="netcortex-log-agent")
    provider = create_log_provider(cfg or {})

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-log-agent",
            "version": "1.0.0",
            "description": "Analyzes log streams for correlated error and timeout patterns.",
            "url": "http://localhost:8002/a2a",
            "endpoint": "http://localhost:8002/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "analyze-logs",
                    "name": "Analyze Logs",
                    "description": "Analyze logs in a time window and produce an AgentFinding.",
                    "tags": ["logs", "errors", "rca"],
                },
                {
                    "id": "respond-to-peer",
                    "name": "Respond To Peer",
                    "description": "Acknowledge peer finding broadcasts. Cross-agent reconsideration "
                    "itself is applied locally by the orchestrator, not here.",
                    "tags": ["a2a", "collaboration"],
                },
            ],
            "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
        }

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        task_id, context_id, data = extract_request_context(payload)
        skill = data["skill"]
        incident_for_log = data.get("incident_id") or data.get("payload", {}).get("incident_id", "")
        logger.info("Received request skill=%s incident=%s", skill, incident_for_log)

        if skill == "analyze-logs":
            logs = provider.get_logs(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
            data_degraded = getattr(provider, "degraded", False)
            error_logs = [l for l in logs if l.level in {"ERROR", "FATAL"}]
            anomaly = len(error_logs) > 0
            summary = "No log anomaly"
            if anomaly:
                services: dict[str, int] = {}
                for entry in error_logs:
                    services[entry.service] = services.get(entry.service, 0) + 1
                top_service = max(services, key=services.get)
                summary = (
                    f"Error patterns in logs: {len(error_logs)} error/fatal events "
                    f"across {len(services)} services; top_service={top_service}"
                )
            logger.info(
                "Analyzed logs region=%s window=%s scenario=%s events=%s anomaly=%s data_degraded=%s",
                data["region"],
                data["window_minutes"],
                data.get("scenario_id"),
                len(logs),
                anomaly,
                data_degraded,
            )
            finding = AgentFinding(
                agent_id="log",
                domain="log",
                anomaly_detected=anomaly,
                summary=summary,
                key_events=[l.model_dump() for l in logs[:5]],
                start_time=min((l.timestamp for l in logs), default=datetime.now(timezone.utc)),
                end_time=max((l.timestamp for l in logs), default=datetime.now(timezone.utc)),
                confidence=0.78 if anomaly else 0.2,
                data_degraded=data_degraded,
            )
            logger.info("Completed analyze-logs anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
            return build_task_result(
                payload=payload,
                task_id=task_id,
                context_id=context_id,
                state="completed",
                artifact_name="agent_finding",
                data=finding.model_dump(mode="json"),
            )

        # Peer finding broadcasts (respond-to-peer) are acknowledged only.
        # Real cross-agent reconsideration is applied locally by the
        # orchestrator (apply_local_reconsideration in
        # core/orchestrator.py's collaboration_node), not via this endpoint.
        logger.info("Acknowledged peer message skill=%s incident=%s", skill, incident_for_log)
        return build_task_result(
            payload=payload,
            task_id=task_id,
            context_id=context_id,
            state="completed",
            artifact_name="peer_response",
            data={"ack": True},
        )

    return app
