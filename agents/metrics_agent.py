from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.metrics_sim import SimulationMetricsProvider


logger = logging.getLogger("net_cortex.agent.metrics")


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


def build_metrics_app() -> FastAPI:
    app = FastAPI(title="netcortex-metrics-agent")
    provider = SimulationMetricsProvider()
    state = {"analysis_in_progress": False, "pending_peer_messages": []}

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
            state["analysis_in_progress"] = True
            metrics = provider.get_metrics(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
            impacted = [m for m in metrics if m.error_rate > 2 or m.packet_loss > 2 or m.throughput_gbps < 0.7]
            anomaly = len(impacted) > 0
            summary = "Metrics within baseline"
            if anomaly:
                worst = max(impacted, key=lambda x: (x.error_rate + x.packet_loss))
                node = worst.tags.get("switch", "unknown")
                summary = (
                    f"Metric anomalies detected on {len(impacted)}/{len(metrics)} nodes; "
                    f"worst switch={node}, error_rate={worst.error_rate:.2f}%, "
                    f"packet_loss={worst.packet_loss:.2f}%, throughput={worst.throughput_gbps:.2f}Gbps"
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
                key_events=[m.model_dump() for m in metrics[:3]],
                start_time=min((m.timestamp for m in metrics), default=datetime.now(timezone.utc)),
                end_time=max((m.timestamp for m in metrics), default=datetime.now(timezone.utc)),
                confidence=0.82 if anomaly else 0.25,
            )
            state["analysis_in_progress"] = False
            logger.info("Completed analyze-metrics anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
            return _task_result(
                payload=payload,
                task_id=task_id,
                context_id=context_id,
                state="completed",
                artifact_name="agent_finding",
                data=finding.model_dump(mode="json"),
            )

        if state["analysis_in_progress"]:
            state["pending_peer_messages"].append(data)
            logger.info("Queued peer message while busy queue_size=%s", len(state["pending_peer_messages"]))
            return _task_result(payload=payload, task_id=task_id, context_id=context_id, state="submitted")

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
