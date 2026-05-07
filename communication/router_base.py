from __future__ import annotations

from typing import Protocol

from models.schemas import A2AMessage, AgentFinding


class RouterBase(Protocol):
    async def send_analysis(
        self,
        agent_id: str,
        incident_id: str,
        skill: str,
        payload_data: dict,
    ) -> AgentFinding:
        ...

    async def send_direct(
        self,
        sender: str,
        target: str,
        message_type: str,
        payload: dict,
        round_number: int,
        session_id: str,
    ) -> A2AMessage:
        ...

    async def broadcast(
        self,
        sender: str,
        message_type: str,
        payload: dict,
        round_number: int,
        session_id: str,
    ) -> list[A2AMessage]:
        ...
