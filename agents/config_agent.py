from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.config_sim import SimulationConfigProvider


logger = logging.getLogger("net_cortex.agent.config")


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


def build_config_app() -> FastAPI:
    app = FastAPI(title="netcortex-config-agent")
    provider = SimulationConfigProvider()
    active_sessions: set[str] = set()
    pending_peer_messages: dict[str, list[dict]] = defaultdict(list)
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
                anomaly = len(changes) > 0
                summary = "No config changes"
                if anomaly:
                    first = changes[0]
                    summary = (
                        f"Config changes found: {len(changes)} change(s); "
                        f"first={first.change_type} on {first.component}"
                    )
                logger.info(
                    "Analyzed config region=%s window=%s scenario=%s changes=%s anomaly=%s",
                    data["region"],
                    data["window_minutes"],
                    data.get("scenario_id"),
                    len(changes),
                    anomaly,
                )
                finding = AgentFinding(
                    agent_id="config",
                    domain="config",
                    anomaly_detected=anomaly,
                    summary=summary,
                    key_events=[c.model_dump() for c in changes[:5]],
                    start_time=min((c.timestamp for c in changes), default=datetime.now(timezone.utc)),
                    end_time=max((c.timestamp for c in changes), default=datetime.now(timezone.utc)),
                    confidence=0.83 if anomaly else 0.2,
                )
            finally:
                async with state_lock:
                    active_sessions.discard(context_id)
                    # TODO: drain pending_peer_messages[context_id] queued during analysis
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
