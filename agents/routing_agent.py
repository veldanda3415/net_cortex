from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.routing_sim import SimulationRoutingProvider


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
            if "Peer corroboration:" not in revised.summary:
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
            if "Peer corroboration:" not in revised.summary:
                revised.summary = (
                    f"{revised.summary}. "
                    f"Peer corroboration: aligned anomalies from [{', '.join(corroborating_domains)}]"
                )
        return revised

    # Routing stability can be meaningful when other domains are noisy.
    if peer_anomalies:
        revised.revised = True
        revised.revision_count += 1
        revised.confidence = max(revised.confidence, 0.78)
        if "Peer contradiction:" not in revised.summary:
            contradictory_domains = sorted({p.domain for p in peer_anomalies})
            revised.summary = (
                f"{revised.summary}. "
                f"Peer contradiction: no routing path change while [{', '.join(contradictory_domains)}] reported anomalies"
            )

    return revised


def _extract_request_context(payload: dict) -> tuple[str, str, dict]:
    params = payload.get("params", {})
    message = params.get("message", {})
    parts = message.get("parts", [])
    data = {}
    for part in parts:
        kind = part.get("kind") or part.get("type")
        if kind == "data" and isinstance(part.get("data"), dict):
            data = part["data"]
            break
    task_id = params.get("id") or params.get("taskId") or f"task-{uuid4()}"
    context_id = params.get("sessionId") or message.get("contextId") or params.get("contextId") or ""
    return str(task_id), str(context_id), data


def _task_result(payload: dict, task_id: str, context_id: str, state: str, artifact_name: str | None = None, data: dict | None = None) -> dict:
    result: dict = {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": state, "timestamp": datetime.now(timezone.utc).isoformat()},
    }
    if artifact_name is not None and data is not None:
        result["artifacts"] = [
            {
                "artifactId": f"artifact-{uuid4()}",
                "name": artifact_name,
                "parts": [{"kind": "data", "data": data}],
            }
        ]
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}


def build_routing_app() -> FastAPI:
    app = FastAPI(title="netcortex-routing-agent")
    provider = SimulationRoutingProvider()
    active_sessions: set[str] = set()
    pending_peer_messages: dict[str, list[dict]] = defaultdict(list)
    session_findings: dict[str, AgentFinding] = {}
    state_lock = asyncio.Lock()

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
                    "description": "Respond to peer clarification and validation requests.",
                    "tags": ["a2a", "collaboration"],
                },
            ],
            "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
        }

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        task_id, context_id, data = _extract_request_context(payload)
        skill = data["skill"]
        incident_for_log = data.get("incident_id") or data.get("payload", {}).get("incident_id", "")
        logger.info("Received request skill=%s incident=%s", skill, incident_for_log)
        if skill == "analyze-routing":
            async with state_lock:
                active_sessions.add(context_id)
            try:
                events = provider.get_routing_events(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
                anomaly = len(events) > 0
                summary = "No routing changes"
                if anomaly:
                    first = events[0]
                    summary = (
                        f"Routing changes found: {len(events)} event(s); "
                        f"first_path={first.path_id}, change={first.change_type}, details={first.details}"
                    )
                logger.info(
                    "Analyzed routing region=%s window=%s scenario=%s events=%s anomaly=%s",
                    data["region"],
                    data["window_minutes"],
                    data.get("scenario_id"),
                    len(events),
                    anomaly,
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
                )
            finally:
                async with state_lock:
                    active_sessions.discard(context_id)
                    queued = pending_peer_messages.pop(context_id, [])

            # Register finding so respond-to-peer can update it during this session
            session_findings[context_id] = finding

            # Process queued peer messages (received while analysis was running)
            for queued_data in queued:
                message_type = queued_data.get("message_type", queued_data.get("skill", ""))
                sender = queued_data.get("sender_agent", "unknown")
                logger.info(
                    "Processing queued peer message sender=%s type=%s incident=%s",
                    sender, message_type, context_id,
                )
                if message_type == "finding_publish":
                    raw = queued_data.get("payload", queued_data)
                    try:
                        peer_finding = AgentFinding.model_validate(raw)
                        current = session_findings.get(context_id)
                        if current:
                            revised = reconsider_finding(current, [peer_finding])
                            session_findings[context_id] = revised
                            logger.info(
                                "Reconsidered finding after drained peer message sender=%s revised=%s",
                                sender, revised.revised,
                            )
                    except Exception:
                        logger.warning("Could not parse peer finding from queued message sender=%s", sender)

            # Use finding as possibly revised by drained peer messages
            finding = session_findings.get(context_id, finding)
            session_findings.pop(context_id, None)  # clean up after use
            logger.info("Completed analyze-routing anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
            return _task_result(
                payload=payload,
                task_id=task_id,
                context_id=context_id,
                state="completed",
                artifact_name="agent_finding",
                data=finding.model_dump(mode="json"),
            )

        async with state_lock:
            busy = context_id in active_sessions
            if busy:
                pending_peer_messages[context_id].append(data)
                queue_size = len(pending_peer_messages[context_id])
            else:
                queue_size = 0
        if busy:
            logger.info("Queued peer message while busy incident=%s queue_size=%s", incident_for_log, queue_size)
            return _task_result(payload=payload, task_id=task_id, context_id=context_id, state="submitted")

        message_type = data.get("message_type", "")
        if skill == "respond-to-peer" and message_type == "finding_publish":
            sender = data.get("sender_agent", "unknown")
            raw = data.get("payload", data)
            try:
                peer_finding = AgentFinding.model_validate(raw)
            except Exception:
                valid_domains = {"metrics", "log", "routing", "config"}
                safe_domain = sender if sender in valid_domains else "metrics"
                peer_finding = AgentFinding(
                    agent_id=sender,
                    domain=safe_domain,
                    anomaly_detected=bool(data.get("payload", {}).get("anomaly_detected", False)),
                    summary=str(data.get("payload", {}).get("summary", "")),
                    key_events=[],
                    start_time=datetime.now(timezone.utc),
                    end_time=datetime.now(timezone.utc),
                    confidence=0.5,
                )
            current_finding = session_findings.get(context_id)
            if current_finding:
                revised = reconsider_finding(current_finding, [peer_finding])
                session_findings[context_id] = revised
                logger.info(
                    "Reconsidered finding after peer message sender=%s revised=%s",
                    sender, revised.revised,
                )
            return _task_result(
                payload=payload,
                task_id=task_id,
                context_id=context_id,
                state="completed",
                artifact_name="peer_response",
                data={"ack": True, "reconsidered": current_finding is not None},
            )

        logger.info("Responded to peer message")
        return _task_result(
            payload=payload,
            task_id=task_id,
            context_id=context_id,
            state="completed",
            artifact_name="peer_response",
            data={"ack": True},
        )

    return app
