from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI

from agents.a2a_protocol import build_task_result, extract_request_context
from models.schemas import AgentFinding
from providers.factory import create_routing_provider


logger = logging.getLogger("net_cortex.agent.routing")


def _throughput_below(event: dict, threshold: float = 0.7) -> bool:
    try:
        return float(event.get("throughput_gbps", 999)) < threshold
    except (TypeError, ValueError):
        return False


def reconsider_finding(finding: AgentFinding, peer_findings: list[AgentFinding]) -> AgentFinding:
    """Adjust routing confidence/summary using peer domain evidence."""
    revised = finding.model_copy(deep=True)
    peer_anomalies = [peer for peer in peer_findings if peer.anomaly_detected]

    if revised.anomaly_detected:
        # Idempotency guard: see metrics_agent.reconsider_finding for why this
        # must gate the whole block, not just the summary text append.
        if "Peer corroboration:" in revised.summary:
            return revised

        is_reroute = "reroute" in revised.summary.lower() or any(
            isinstance(event, dict) and str(event.get("change_type", "")).lower() == "reroute"
            for event in revised.key_events
        )
        metrics_throughput_drop = any(
            peer.agent_id == "metrics"
            and any(
                isinstance(event, dict) and _throughput_below(event)
                for event in peer.key_events
            )
            for peer in peer_findings
        )
        if is_reroute and metrics_throughput_drop:
            revised.revised = True
            revised.revision_count += 1
            revised.confidence = max(revised.confidence, 0.91)
            revised.summary = (
                f"{revised.summary}. "
                "Peer corroboration: metrics reported throughput drop during routing reroute"
            )
            return revised

        corroborating_domains = sorted({p.domain for p in peer_anomalies})
        if corroborating_domains:
            revised.revised = True
            revised.revision_count += 1
            revised.confidence = min(0.94, revised.confidence + 0.04)
            revised.summary = (
                f"{revised.summary}. "
                f"Peer corroboration: aligned anomalies from [{', '.join(corroborating_domains)}]"
            )
        return revised

    # Routing stability can be meaningful when other domains are noisy.
    if peer_anomalies and "Peer contradiction:" not in revised.summary:
        revised.revised = True
        revised.revision_count += 1
        revised.confidence = max(revised.confidence, 0.78)
        contradictory_domains = sorted({p.domain for p in peer_anomalies})
        revised.summary = (
            f"{revised.summary}. "
            f"Peer contradiction: no routing path change while [{', '.join(contradictory_domains)}] reported anomalies"
        )

    return revised


def build_routing_app(cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="netcortex-routing-agent")
    provider = create_routing_provider(cfg or {})

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-routing-agent",
            "version": "1.0.0",
            "description": "Analyzes routing events and topology changes around incidents.",
            "url": "http://localhost:8003/a2a",
            "endpoint": "http://localhost:8003/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "analyze-routing",
                    "name": "Analyze Routing",
                    "description": "Analyze routing events in a time window and produce an AgentFinding.",
                    "tags": ["routing", "topology", "rca"],
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

        if skill == "analyze-routing":
            events = provider.get_routing_events(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
            data_degraded = getattr(provider, "degraded", False)
            anomaly = len(events) > 0
            summary = "No routing changes"
            if anomaly:
                first = events[0]
                summary = (
                    f"Routing changes found: {len(events)} event(s); "
                    f"first_path={first.path_id}, change={first.change_type}, details={first.details}"
                )
            logger.info(
                "Analyzed routing region=%s window=%s scenario=%s events=%s anomaly=%s data_degraded=%s",
                data["region"],
                data["window_minutes"],
                data.get("scenario_id"),
                len(events),
                anomaly,
                data_degraded,
            )
            finding = AgentFinding(
                agent_id="routing",
                domain="routing",
                anomaly_detected=anomaly,
                summary=summary,
                key_events=[e.model_dump() for e in events[:5]],
                start_time=min((e.timestamp for e in events), default=datetime.now(timezone.utc)),
                end_time=max((e.timestamp for e in events), default=datetime.now(timezone.utc)),
                confidence=0.8 if anomaly else 0.15,
                data_degraded=data_degraded,
            )
            logger.info("Completed analyze-routing anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
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
