from __future__ import annotations

from fastapi import FastAPI

from ingestion.incident_normalizer import normalize_incident
from models.schemas import RCAReport


class WebhookServer:
    def __init__(self):
        self.app = FastAPI(title="netcortex-ingestion")
        self._handler = None

        @self.app.post("/incidents")
        async def ingest(payload: dict) -> dict:
            if self._handler is None:
                return {"error": "handler_not_configured"}
            incident = normalize_incident(payload)
            report: RCAReport = await self._handler(incident)
            return report.model_dump(mode="json")

    def set_handler(self, handler):
        self._handler = handler
