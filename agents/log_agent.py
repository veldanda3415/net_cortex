from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.log_sim import SimulationLogProvider


logger = logging.getLogger("net_cortex.agent.log")


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


def build_log_app() -> FastAPI:
    app = FastAPI(title="netcortex-log-agent")
    provider = SimulationLogProvider()
    state = {"analysis_in_progress": False, "pending_peer_messages": []}

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
        if skill == "analyze-logs":
            state["analysis_in_progress"] = True
            logs = provider.get_logs(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
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
                "Analyzed logs region=%s window=%s scenario=%s events=%s anomaly=%s",
                data["region"],
                data["window_minutes"],
                data.get("scenario_id"),
                len(logs),
                anomaly,
            )
            finding = AgentFinding(
                agent_id="log",
                domain="logs",
                anomaly_detected=anomaly,
                summary=summary,
                key_events=[l.model_dump() for l in logs[:5]],
                start_time=min((l.timestamp for l in logs), default=datetime.now(timezone.utc)),
                end_time=max((l.timestamp for l in logs), default=datetime.now(timezone.utc)),
                confidence=0.78 if anomaly else 0.2,
            )
            state["analysis_in_progress"] = False
            logger.info("Completed analyze-logs anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
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
