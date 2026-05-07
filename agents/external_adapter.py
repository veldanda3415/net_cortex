from __future__ import annotations

from communication.router_base import RouterBase
from models.schemas import IncidentRequest


class ExternalAgentAdapterNode:
    def __init__(self, agent_id: str, endpoint: str, domain_skill: str, router: RouterBase):
        self.agent_id = agent_id
        self.endpoint = endpoint
        self.domain_skill = domain_skill
        self.router = router

    async def __call__(self, incident: IncidentRequest) -> dict:
        finding = await self.router.send_analysis(
            agent_id=self.agent_id,
            incident_id=incident.incident_id,
            skill=self.domain_skill,
            payload_data={
                "region": incident.region,
                "window_minutes": 30,
                "incident_id": incident.incident_id,
                "scenario_id": incident.scenario_id,
            },
        )
        return {"findings": [finding]}
