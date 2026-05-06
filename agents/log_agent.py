from __future__ import annotations

import logging
from datetime import datetime, timezone
from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.log_sim import SimulationLogProvider


logger = logging.getLogger("net_cortex.agent.log")


def build_log_app() -> FastAPI:
    app = FastAPI(title="netcortex-log-agent")
    provider = SimulationLogProvider()
    state = {"analysis_in_progress": False, "pending_peer_messages": []}

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-log-agent",
            "version": "1.0.0",
            "endpoint": "http://localhost:8002/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [{"id": "analyze-logs"}, {"id": "respond-to-peer"}],
            "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
        }

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        data = payload["params"]["message"]["parts"][1]["data"]
        skill = data["skill"]
        logger.info("Received request skill=%s incident=%s", skill, data.get("incident_id", ""))
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
            return {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "id": payload["params"]["id"],
                    "sessionId": payload["params"]["sessionId"],
                    "status": {"state": "completed", "timestamp": datetime.now(timezone.utc).isoformat()},
                    "artifacts": [{"name": "agent_finding", "parts": [{"type": "data", "data": finding.model_dump(mode='json')}]}],
                },
            }

        if state["analysis_in_progress"]:
            state["pending_peer_messages"].append(data)
            logger.info("Queued peer message while busy queue_size=%s", len(state["pending_peer_messages"]))
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"status": {"state": "queued"}}}

        logger.info("Responded to peer message")
        return {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "id": payload["params"]["id"],
                "sessionId": payload["params"]["sessionId"],
                "status": {"state": "completed", "timestamp": datetime.now(timezone.utc).isoformat()},
                "artifacts": [{"name": "peer_response", "parts": [{"type": "data", "data": {"ack": True}}]}],
            },
        }

    return app
