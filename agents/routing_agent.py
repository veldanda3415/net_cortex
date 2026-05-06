from __future__ import annotations

import logging
from datetime import datetime, timezone
from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.routing_sim import SimulationRoutingProvider


logger = logging.getLogger("net_cortex.agent.routing")


def build_routing_app() -> FastAPI:
    app = FastAPI(title="netcortex-routing-agent")
    provider = SimulationRoutingProvider()
    state = {"analysis_in_progress": False, "pending_peer_messages": []}

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-routing-agent",
            "version": "1.0.0",
            "endpoint": "http://localhost:8003/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [{"id": "analyze-routing"}, {"id": "respond-to-peer"}],
            "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
        }

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        data = payload["params"]["message"]["parts"][1]["data"]
        skill = data["skill"]
        logger.info("Received request skill=%s incident=%s", skill, data.get("incident_id", ""))
        if skill == "analyze-routing":
            state["analysis_in_progress"] = True
            events = provider.get_routing_events(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
            anomaly = len(events) > 0
            summary = "No routing changes"
            if anomaly:
                first = events[0]
                summary = (
                    f"Routing changes found: {len(events)} event(s); "
                    f"first_path={first.path_id}, from={first.before_hops} to={first.after_hops} hops"
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
            state["analysis_in_progress"] = False
            logger.info("Completed analyze-routing anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
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
