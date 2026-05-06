from __future__ import annotations

import logging
from datetime import datetime, timezone
from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.config_sim import SimulationConfigProvider


logger = logging.getLogger("net_cortex.agent.config")


def build_config_app() -> FastAPI:
    app = FastAPI(title="netcortex-config-agent")
    provider = SimulationConfigProvider()
    state = {"analysis_in_progress": False, "pending_peer_messages": []}

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-config-agent",
            "version": "1.0.0",
            "endpoint": "http://localhost:8004/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [{"id": "analyze-config"}, {"id": "respond-to-peer"}],
            "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
        }

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        data = payload["params"]["message"]["parts"][1]["data"]
        skill = data["skill"]
        logger.info("Received request skill=%s incident=%s", skill, data.get("incident_id", ""))
        if skill == "analyze-config":
            state["analysis_in_progress"] = True
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
            state["analysis_in_progress"] = False
            logger.info("Completed analyze-config anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
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
