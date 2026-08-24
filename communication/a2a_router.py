from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from communication.agent_registry import AgentRegistry
from communication.message_types import JsonRpcMessage, JsonRpcPart, TaskParams, TaskRequest
from models.schemas import A2AMessage, AgentFinding

logger = logging.getLogger("net_cortex.a2a_router")


class A2ARouter:
    def __init__(self, registry: AgentRegistry, message_timeout_seconds: int) -> None:
        self.registry = registry
        self.message_timeout_seconds = message_timeout_seconds

    async def _post_task(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(self.message_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            return response.json()

    async def send_analysis(self, agent_id: str, incident_id: str, skill: str, payload_data: dict[str, Any]) -> AgentFinding:
        endpoint = self.registry.agents[agent_id].endpoint
        req = TaskRequest(
            id=f"req-{uuid4()}",
            params=TaskParams(
                id=f"task-{agent_id}-{incident_id}",
                sessionId=incident_id,
                message=JsonRpcMessage(
                    parts=[
                        JsonRpcPart(type="text", text=f"Run {skill}"),
                        JsonRpcPart(type="data", data={"skill": skill, **payload_data}),
                    ]
                ),
            ),
        )
        resp = await self._post_task(endpoint, req.model_dump())
        artifact_data = resp["result"]["artifacts"][0]["parts"][0]["data"]
        return AgentFinding.model_validate(artifact_data)

    async def send_direct(self, sender: str, target: str, message_type: str, payload: dict[str, Any], round_number: int, session_id: str) -> A2AMessage:
        endpoint = self.registry.agents[target].endpoint
        req = TaskRequest(
            id=f"req-{uuid4()}",
            params=TaskParams(
                id=f"task-a2a-{sender}-to-{target}-r{round_number}",
                sessionId=session_id,
                message=JsonRpcMessage(
                    parts=[
                        JsonRpcPart(type="text", text=message_type),
                        JsonRpcPart(
                            type="data",
                            data={
                                "skill": "respond-to-peer",
                                "message_type": message_type,
                                "sender_agent": sender,
                                "round_number": round_number,
                                "payload": payload,
                            },
                        ),
                    ]
                ),
            ),
        )
        try:
            resp = await self._post_task(endpoint, req.model_dump())
        except Exception as exc:
            logger.warning(
                "A2A send_direct failed sender=%s target=%s message_type=%s round=%s error=%s",
                sender, target, message_type, round_number, exc,
            )
            status = "failed"
        else:
            status_block = resp.get("result", {}).get("status")
            if not isinstance(status_block, dict) or "state" not in status_block:
                # Missing/malformed status must not silently read as success —
                # a well-formed agent response always has status.state (see
                # agents/a2a_protocol.py:build_task_result).
                logger.warning(
                    "A2A send_direct got a response with no status.state sender=%s target=%s response=%s",
                    sender, target, resp,
                )
                status = "failed"
            else:
                normalized = str(status_block["state"]).lower()
                if normalized in {"completed", "queued", "working", "submitted"}:
                    status = normalized
                elif normalized in {"canceled", "cancelled"}:
                    status = "cancelled"
                else:
                    status = "failed"

        return A2AMessage(
            sender_agent=sender,
            target_agent=target,
            message_type=message_type,
            payload={"status": status, **payload},
            round_number=round_number,
            timestamp=datetime.now(timezone.utc),
        )

    async def broadcast(self, sender: str, message_type: str, payload: dict[str, Any], round_number: int, session_id: str) -> list[A2AMessage]:
        tasks = []
        for target in self.registry.agents:
            if target == sender:
                continue
            tasks.append(self.send_direct(sender, target, message_type, payload, round_number, session_id))

        if not tasks:
            return []
        return await asyncio.gather(*tasks)
