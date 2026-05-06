from __future__ import annotations

import logging
from datetime import datetime, timezone
from fastapi import FastAPI

from models.schemas import AgentFinding
from providers.simulation.metrics_sim import SimulationMetricsProvider


logger = logging.getLogger("net_cortex.agent.metrics")


def build_metrics_app() -> FastAPI:
    app = FastAPI(title="netcortex-metrics-agent")
    provider = SimulationMetricsProvider()
    state = {"analysis_in_progress": False, "pending_peer_messages": []}

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-metrics-agent",
            "version": "1.0.0",
            "endpoint": "http://localhost:8001/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [{"id": "analyze-metrics"}, {"id": "respond-to-peer"}],
            "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
        }

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        data = payload["params"]["message"]["parts"][1]["data"]
        skill = data["skill"]
        logger.info("Received request skill=%s incident=%s", skill, data.get("incident_id", ""))
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
