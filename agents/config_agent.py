from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.adapters.prometheus_baseline_adapter import PrometheusBaselineProvider
from providers.baseline_utils import compute_z_score, is_anomalous
from providers.simulation.baseline_sim import SimulationBaselineProvider
from providers.simulation.config_sim import SimulationConfigProvider


logger = logging.getLogger("net_cortex.agent.config")


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "have", "has", "had",
    "into", "onto", "about", "after", "before", "during", "high", "low", "drop", "spike", "region",
    "incident", "service", "network",
}


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
        if len(token) >= 4 and token not in _STOPWORDS
    }


def _is_change_relevant(change, incident_description: str) -> bool:
    if not incident_description.strip():
        return True

    incident_tokens = _tokenize(incident_description)
    if not incident_tokens:
        return True

    change_blob = (
        f"{change.component} {change.change_type} "
        f"{' '.join(str(k) for k in change.before.keys())} {' '.join(str(v) for v in change.before.values())} "
        f"{' '.join(str(k) for k in change.after.keys())} {' '.join(str(v) for v in change.after.values())}"
    )
    change_tokens = _tokenize(change_blob)

    if incident_tokens & change_tokens:
        return True

    # Network symptom incidents are often caused by policy/bandwidth config changes
    # even when the textual overlap is weak.
    has_network_symptom = any(k in incident_description.lower() for k in ("throughput", "latency", "packet", "error", "loss"))
    if has_network_symptom and change.change_type in {"policy_update", "bandwidth_limit", "rollback"}:
        return True

    return False


def reconsider_finding(finding: AgentFinding, peer_findings: list[AgentFinding]) -> AgentFinding:
    """Adjust config confidence/summary using peer domain evidence."""
    revised = finding.model_copy(deep=True)

    peer_anomalies = [peer for peer in peer_findings if peer.anomaly_detected]
    if revised.anomaly_detected:
        if peer_anomalies:
            revised.revised = True
            revised.revision_count += 1
            revised.confidence = min(0.95, revised.confidence + 0.04)
        return revised

    # No config change found while peers report anomalies is valuable contradictory evidence.
    if peer_anomalies:
        revised.revised = True
        revised.revision_count += 1
        revised.confidence = max(revised.confidence, 0.86)
        if "Peer contradiction:" not in revised.summary:
            domains = ", ".join(sorted({p.domain for p in peer_anomalies}))
            revised.summary = (
                f"{revised.summary}. "
                f"Peer contradiction: anomalies observed in [{domains}] while config remains unchanged"
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


def build_config_app(cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="netcortex-config-agent")
    provider = SimulationConfigProvider()
    baseline_cfg = (cfg or {}).get("baselines", {})
    baseline_provider_name = str(baseline_cfg.get("provider", "simulation")).lower()
    if baseline_provider_name == "simulation":
        baseline_provider = SimulationBaselineProvider()
    elif baseline_provider_name == "prometheus":
        baseline_provider = PrometheusBaselineProvider()
    else:
        raise ValueError("ConfigValidationError: baselines.provider must be either 'simulation' or 'prometheus'")
    z_threshold = float(baseline_cfg.get("config_z_threshold", 2.5))
    legacy_fallback = bool(baseline_cfg.get("legacy_fallback", True))
    active_sessions: set[str] = set()
    pending_peer_messages: dict[str, list[dict]] = defaultdict(list)
    session_findings: dict[str, AgentFinding] = {}
    state_lock = asyncio.Lock()

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-config-agent",
            "version": "1.0.0",
            "description": "Analyzes configuration changes proximate to incident windows.",
            "url": "http://localhost:8004/a2a",
            "endpoint": "http://localhost:8004/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "analyze-config",
                    "name": "Analyze Config",
                    "description": "Analyze config changes in a time window and produce an AgentFinding.",
                    "tags": ["config", "changes", "rca"],
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
        if skill == "analyze-config":
            async with state_lock:
                active_sessions.add(context_id)
            try:
                changes = provider.get_config_changes(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
                incident_description = str(data.get("incident_description", ""))
                relevant_changes = [c for c in changes if _is_change_relevant(c, incident_description)]
                component_counts: dict[str, int] = defaultdict(int)
                for change in relevant_changes:
                    component_counts[change.component] += 1

                anomalous_components: list[tuple[str, float]] = []
                baseline_hits = 0
                for component, count in component_counts.items():
                    baseline = baseline_provider.get_baseline(f"component:{component}", "change_count")
                    if baseline is None:
                        baseline = baseline_provider.get_baseline(f"region:{data['region']}", "change_count")
                    if baseline is None:
                        continue
                    baseline_hits += 1
                    z_score = compute_z_score(float(count), baseline)
                    if is_anomalous(float(count), baseline, z_threshold=z_threshold):
                        anomalous_components.append((component, z_score))

                # Primary responsibility: if incident-window relevant changes exist, surface them directly.
                if len(relevant_changes) > 0:
                    anomaly = True
                    latest = max(relevant_changes, key=lambda c: c.timestamp)
                    summary = (
                        f"Incident-relevant config changes found: {len(relevant_changes)} change(s); "
                        f"latest={latest.change_type} on {latest.component}"
                    )
                    if anomalous_components:
                        top_component, top_z = max(anomalous_components, key=lambda item: item[1])
                        summary = (
                            f"{summary}; volume anomaly on {top_component} z={top_z:.2f}"
                        )
                else:
                    if baseline_hits == 0:
                        anomaly = False if not legacy_fallback else False
                    else:
                        anomaly = bool(anomalous_components)

                    summary = "No incident-relevant config changes"
                    if len(changes) > 0 and len(relevant_changes) == 0:
                        summary = f"Config changes present ({len(changes)}) but not incident-relevant"
                    if anomaly and anomalous_components:
                        top_component, top_z = max(anomalous_components, key=lambda item: item[1])
                        summary = (
                            f"Config change volume anomaly detected without direct change records; "
                            f"top_component={top_component} z={top_z:.2f}"
                        )
                logger.info(
                    "Analyzed config region=%s window=%s scenario=%s changes=%s relevant_changes=%s anomaly=%s",
                    data["region"],
                    data["window_minutes"],
                    data.get("scenario_id"),
                    len(changes),
                    len(relevant_changes),
                    anomaly,
                )
                finding = AgentFinding(
                    agent_id="config",
                    domain="config",
                    anomaly_detected=anomaly,
                    summary=summary,
                    key_events=[c.model_dump() for c in relevant_changes[:5]],
                    start_time=min((c.timestamp for c in relevant_changes), default=datetime.now(timezone.utc)),
                    end_time=max((c.timestamp for c in relevant_changes), default=datetime.now(timezone.utc)),
                    confidence=(
                        min(0.95, 0.75 + 0.1 * max((z for _, z in anomalous_components), default=0.0))
                        if len(relevant_changes) > 0
                        else (min(0.9, 0.6 + 0.1 * max((z for _, z in anomalous_components), default=0.0)) if anomaly else 0.2)
                    ),
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
            logger.info("Completed analyze-config anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
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
