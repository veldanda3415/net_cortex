from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from ingestion.incident_normalizer import normalize_incident
from models.schemas import RCAReport

logger = logging.getLogger("net_cortex.ingestion.webhook")


class WebhookServer:
    def __init__(self, cfg: dict | None = None):
        self.app = FastAPI(title="netcortex-ingestion")
        self._handler = None

        auth_cfg = (cfg or {}).get("ingestion", {}).get("auth", {}) or {}
        auth_enabled = bool(auth_cfg.get("enabled", False))
        token_env = str(auth_cfg.get("token_env", "NETCORTEX_WEBHOOK_TOKEN"))
        auth_token = os.environ.get(token_env, "")
        if auth_enabled and not auth_token:
            raise ValueError(
                f"ConfigValidationError: ingestion.auth.enabled=true but env var {token_env} is not set"
            )
        if not auth_enabled:
            logger.warning(
                "Webhook ingestion auth DISABLED (ingestion.auth.enabled is false/unset) — "
                "/incidents accepts unauthenticated requests. Do not expose this outside local demos."
            )

        @self.app.post("/incidents")
        async def ingest(request: Request, payload: dict) -> dict:
            if auth_enabled:
                scheme, _, token = request.headers.get("authorization", "").partition(" ")
                if scheme.lower() != "bearer" or token != auth_token:
                    raise HTTPException(status_code=401, detail="unauthorized")

            if self._handler is None:
                raise HTTPException(status_code=503, detail="handler_not_configured")

            try:
                incident = normalize_incident(payload)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors())

            report: RCAReport = await self._handler(incident)
            return report.model_dump(mode="json")

    def set_handler(self, handler):
        self._handler = handler
