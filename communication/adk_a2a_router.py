from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from communication.agent_registry import AgentRegistry
from models.schemas import A2AMessage, AgentFinding


class ADKA2ARouter:
    def __init__(self, registry: AgentRegistry, message_timeout_seconds: int) -> None:
        self.registry = registry
        self.message_timeout_seconds = message_timeout_seconds
        self._client_factory: Any | None = None
        self._clients: dict[str, Any] = {}

    @staticmethod
    def _require_symbol(module_name: str, symbol: str) -> Any:
        mod = importlib.import_module(module_name)
        return getattr(mod, symbol)

    def _ensure_factory(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory
        client_module = self._require_symbol("a2a.client.client", "ClientConfig")
        factory_module = self._require_symbol("a2a.client.client_factory", "ClientFactory")
        http_client = httpx.AsyncClient(timeout=float(self.message_timeout_seconds))
        cfg = client_module(
            httpx_client=http_client,
            streaming=False,
            polling=False,
        )
        self._client_factory = factory_module(config=cfg)
        return self._client_factory

    def _to_agent_card(self, card_data: dict[str, Any], endpoint: str) -> Any:
        agent_card_cls = self._require_symbol("a2a.types", "AgentCard")
        candidate = dict(card_data)
        candidate.setdefault("url", endpoint)
        candidate.setdefault("defaultInputModes", ["text/plain", "application/json"])
        candidate.setdefault("defaultOutputModes", ["application/json"])
        candidate.setdefault("capabilities", {})
        return agent_card_cls.model_validate(candidate)

    def _get_client(self, agent_id: str):
        cached = self._clients.get(agent_id)
        if cached is not None:
            return cached
        reg = self.registry.agents[agent_id]
        card = self._to_agent_card(reg.card, reg.endpoint)
        client = self._ensure_factory().create(card)
        self._clients[agent_id] = client
        return client

    @staticmethod
    def _extract_data_from_task(task: Any) -> dict[str, Any]:
        for artifact in task.artifacts or []:
            for part in artifact.parts:
                payload = part.model_dump(mode="json")
                if payload.get("kind") == "data" and isinstance(payload.get("data"), dict):
                    return payload["data"]
        return {}

    @staticmethod
    def _extract_data_from_message(message: Any) -> dict[str, Any]:
        for part in message.parts:
            payload = part.model_dump(mode="json")
            if payload.get("kind") == "data" and isinstance(payload.get("data"), dict):
                return payload["data"]
        return {}

    async def _send(self, agent_id: str, data: dict[str, Any], text: str, session_id: str) -> Any:
        message_cls = self._require_symbol("a2a.types", "Message")
        part_cls = self._require_symbol("a2a.types", "Part")
        text_part_cls = self._require_symbol("a2a.types", "TextPart")
        data_part_cls = self._require_symbol("a2a.types", "DataPart")
        role_cls = self._require_symbol("a2a.types", "Role")
        client = self._get_client(agent_id)
        request = message_cls(
            messageId=f"msg-{uuid4()}",
            taskId=None,
            contextId=session_id,
            role=role_cls.user,
            parts=[
                part_cls(root=text_part_cls(text=text)),
                part_cls(root=data_part_cls(data=data)),
            ],
        )

        last_event: Any | None = None
        async for event in client.send_message(request):
            if isinstance(event, tuple):
                last_event = event[0]
            else:
                last_event = event

        if last_event is None:
            raise RuntimeError(f"No response received from agent '{agent_id}'")
        return last_event

    async def send_analysis(self, agent_id: str, incident_id: str, skill: str, payload_data: dict[str, Any]) -> AgentFinding:
        task_cls = self._require_symbol("a2a.types", "Task")
        result = await self._send(
            agent_id=agent_id,
            data={"skill": skill, **payload_data},
            text=f"Run {skill}",
            session_id=incident_id,
        )

        if isinstance(result, task_cls):
            artifact_data = self._extract_data_from_task(result)
        else:
            artifact_data = self._extract_data_from_message(result)
        return AgentFinding.model_validate(artifact_data)

    async def send_direct(self, sender: str, target: str, message_type: str, payload: dict[str, Any], round_number: int, session_id: str) -> A2AMessage:
        task_cls = self._require_symbol("a2a.types", "Task")
        status = "failed"
        try:
            result = await self._send(
                agent_id=target,
                data={
                    "skill": "respond-to-peer",
                    "message_type": message_type,
                    "sender_agent": sender,
                    "round_number": round_number,
                    "payload": payload,
                },
                text=message_type,
                session_id=session_id,
            )
            if isinstance(result, task_cls):
                state = (result.status.state.value if result.status and result.status.state else "unknown").lower()
                if state == "completed":
                    status = "completed"
                elif state in {"canceled", "cancelled"}:
                    status = "cancelled"
                elif state in {"failed", "rejected", "unknown"}:
                    status = "failed"
                else:
                    status = state
            else:
                status = "completed"
        except Exception:
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
