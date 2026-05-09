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
from providers.simulation.metrics_sim import SimulationMetricsProvider


logger = logging.getLogger("net_cortex.agent.metrics")


def _throughput_below(event: dict, threshold: float = 0.7) -> bool:
    try:
        return float(event.get("throughput_gbps", 999)) < threshold
    except (TypeError, ValueError):
        return False


def _metric_entity_keys(metric_row: dict) -> list[str]:
    tags = metric_row.get("tags", {}) if isinstance(metric_row.get("tags", {}), dict) else {}
    keys: list[str] = []
    for tag_name in ("switch", "interface", "uplink", "dst_prefix", "core", "lag", "service"):
        tag_value = tags.get(tag_name)
        if tag_value:
            keys.append(f"{tag_name}:{tag_value}")
    region = metric_row.get("region")
    if region:
        keys.append(f"region:{region}")
    return keys


def reconsider_finding(finding: AgentFinding, peer_findings: list[AgentFinding]) -> AgentFinding:
    """Adjust metrics confidence/summary using peer domain evidence."""
    revised = finding.model_copy(deep=True)
    if not revised.anomaly_detected:
        return revised

    throughput_drop = any(
        isinstance(event, dict) and _throughput_below(event)
        for event in revised.key_events
    )
    routing_reroute = any(
        peer.agent_id == "routing"
        and peer.anomaly_detected
        and (
            "reroute" in peer.summary.lower()
            or any(
                isinstance(event, dict) and str(event.get("change_type", "")).lower() == "reroute"
                for event in peer.key_events
            )
        )
        for peer in peer_findings
    )

    if throughput_drop and routing_reroute:
        revised.revised = True
        revised.revision_count += 1
        revised.confidence = max(revised.confidence, 0.92)
        if "Peer corroboration:" not in revised.summary:
            revised.summary = (
                f"{revised.summary}. "
                "Peer corroboration: routing agent reported reroute during throughput drop; "
                "metrics confidence elevated to 0.92"
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


def build_metrics_app(cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="netcortex-metrics-agent")
    provider = SimulationMetricsProvider()
    baseline_cfg = (cfg or {}).get("baselines", {})
    baseline_provider_name = str(baseline_cfg.get("provider", "simulation")).lower()
    if baseline_provider_name == "simulation":
        baseline_provider = SimulationBaselineProvider()
    elif baseline_provider_name == "prometheus":
        baseline_provider = PrometheusBaselineProvider()
    else:
        raise ValueError("ConfigValidationError: baselines.provider must be either 'simulation' or 'prometheus'")
    z_threshold = float(baseline_cfg.get("metrics_z_threshold", 3.0))
    legacy_fallback = bool(baseline_cfg.get("legacy_fallback", True))
    active_sessions: set[str] = set()
    pending_peer_messages: dict[str, list[dict]] = defaultdict(list)
    session_findings: dict[str, AgentFinding] = {}
    state_lock = asyncio.Lock()

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-metrics-agent",
            "version": "1.0.0",
            "description": "Analyzes metric time-series for anomaly windows and degradation signals.",
            "url": "http://localhost:8001/a2a",
            "endpoint": "http://localhost:8001/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "analyze-metrics",
                    "name": "Analyze Metrics",
                    "description": "Analyze metrics in a time window and produce an AgentFinding.",
                    "tags": ["metrics", "anomaly", "rca"],
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
        if skill == "analyze-metrics":
            async with state_lock:
                active_sessions.add(context_id)
            try:
                metrics = provider.get_metrics(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
                impacted: list[tuple[dict, list[str]]] = []
                for metric in metrics:
                    metric_row = metric.model_dump(mode="json")
                    entity_keys = _metric_entity_keys(metric_row)
                    reasons: list[str] = []
                    had_baseline = False
                    for metric_name, value in (
                        ("error_rate", metric.error_rate),
                        ("packet_loss", metric.packet_loss),
                        ("throughput_gbps", metric.throughput_gbps),
                    ):
                        selected = None
                        for key in entity_keys:
                            candidate = baseline_provider.get_baseline(key, metric_name)
                            if candidate is not None:
                                selected = candidate
                                break
                        if selected is None:
                            continue
                        had_baseline = True
                        z_score = compute_z_score(float(value), selected)
                        if is_anomalous(float(value), selected, z_threshold=z_threshold):
                            reasons.append(f"{metric_name} z={z_score:.2f}")

                    # Fallback keeps behavior safe when no baseline exists for an entity.
                    if legacy_fallback and not had_baseline and (metric.error_rate > 2 or metric.packet_loss > 2 or metric.throughput_gbps < 0.7):
                        reasons.append("legacy-threshold")

                    if reasons:
                        impacted.append((metric_row, reasons))
                anomaly = len(impacted) > 0
                summary = "Metrics within baseline"
                if anomaly:
                    worst, worst_reasons = max(impacted, key=lambda x: (float(x[0].get("error_rate", 0)) + float(x[0].get("packet_loss", 0))))
                    tags = worst.get("tags", {}) if isinstance(worst.get("tags", {}), dict) else {}
                    node = tags.get("switch", "unknown")
                    summary = (
                        f"Metric anomalies detected on {len(impacted)}/{len(metrics)} nodes; "
                        f"worst switch={node}, error_rate={float(worst.get('error_rate', 0.0)):.2f}%, "
                        f"packet_loss={float(worst.get('packet_loss', 0.0)):.2f}%, throughput={float(worst.get('throughput_gbps', 0.0)):.2f}Gbps; "
                        f"evidence={', '.join(worst_reasons)}"
                    )
                logger.info(
                    "Analyzed metrics region=%s window=%s scenario=%s points=%s anomaly=%s",
                    data["region"],
                    data["window_minutes"],
                    data.get("scenario_id"),
                    len(metrics),
                    anomaly,
                )
                finding = AgentFinding(
                    agent_id="metrics",
                    domain="metrics",
                    anomaly_detected=anomaly,
                    summary=summary,
                    key_events=[row for row, _ in impacted[:3]] if anomaly else [m.model_dump(mode="json") for m in metrics[:3]],
                    start_time=min((m.timestamp for m in metrics), default=datetime.now(timezone.utc)),
                    end_time=max((m.timestamp for m in metrics), default=datetime.now(timezone.utc)),
                    confidence=min(0.95, 0.6 + 0.08 * len(impacted)) if anomaly else 0.25,
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
            logger.info("Completed analyze-metrics anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
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
