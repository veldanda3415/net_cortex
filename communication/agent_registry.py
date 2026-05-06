from __future__ import annotations

from dataclasses import dataclass

import httpx


class AgentRegistrationError(RuntimeError):
    pass


@dataclass
class RegisteredAgent:
    agent_id: str
    endpoint: str
    card: dict
    status: str = "ready"


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, RegisteredAgent] = {}

    @property
    def agents(self) -> dict[str, RegisteredAgent]:
        return self._agents

    async def register_from_card(self, agent_id: str, card_url: str, endpoint: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(card_url)
            resp.raise_for_status()
            card = resp.json()

        skills = {s.get("id") for s in card.get("skills", [])}
        contract = card.get("schemaContract", {})
        if contract.get("outputSchema") != "AgentFinding":
            raise AgentRegistrationError(f"{agent_id}: outputSchema must be AgentFinding")

        required_analysis_skill = {
            "metrics": "analyze-metrics",
            "log": "analyze-logs",
            "routing": "analyze-routing",
            "config": "analyze-config",
        }.get(agent_id)
        if required_analysis_skill and required_analysis_skill not in skills:
            raise AgentRegistrationError(f"{agent_id}: missing {required_analysis_skill} skill")

        if "respond-to-peer" not in skills:
            raise AgentRegistrationError(f"{agent_id}: missing respond-to-peer skill")

        self._agents[agent_id] = RegisteredAgent(agent_id=agent_id, endpoint=endpoint, card=card)
