"""Generic domain agent, instantiated per domain from the detection catalog.

One implementation serves every domain in contract/catalog/domains.json:
fetch the domain's telemetry over MCP, evaluate catalog rules
(detection.analyze_domain), return an AgentFinding over A2A. The agent
card (name, port, skill) also comes from the catalog, so the built-in
metrics/log/routing/config agents keep their existing identities and a
new domain needs no code.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from agents.a2a_protocol import build_task_result, extract_request_context
from contract.catalog_loader import Catalog, get_catalog
from detection import analyze_domain
from providers.adapters.prometheus_baseline_adapter import PrometheusBaselineProvider
from providers.base import BaselineProvider
from providers.factory import create_telemetry_provider
from providers.query import query_from_request
from providers.simulation.baseline_sim import SimulationBaselineProvider


def _baseline_provider(cfg: dict[str, Any]) -> BaselineProvider:
    name = str(cfg.get("baselines", {}).get("provider", "simulation")).lower()
    if name == "simulation":
        return SimulationBaselineProvider()
    if name == "prometheus":
        return PrometheusBaselineProvider()
    raise ValueError("ConfigValidationError: baselines.provider must be either 'simulation' or 'prometheus'")


def agent_card(catalog: Catalog, domain_id: str) -> dict[str, Any]:
    agent = catalog.domain(domain_id).agent
    url = f"http://localhost:{agent.port}/a2a"
    return {
        "name": agent.name,
        "version": "1.0.0",
        "description": agent.description,
        "url": url,
        "endpoint": url,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            agent.skill.model_dump(),
            {
                "id": "respond-to-peer",
                "name": "Respond To Peer",
                "description": "Acknowledge peer finding broadcasts. Cross-agent reconsideration "
                "itself is applied locally by the orchestrator, not here.",
                "tags": ["a2a", "collaboration"],
            },
        ],
        "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
    }


def build_domain_app(domain_id: str, cfg: dict | None = None, catalog: Catalog | None = None) -> FastAPI:
    cfg = cfg or {}
    catalog = catalog or get_catalog()
    domain = catalog.domain(domain_id)  # KeyError -> unknown domain, fail at startup
    logger = logging.getLogger(f"net_cortex.agent.{domain_id}")

    app = FastAPI(title=f"netcortex-{domain_id}-agent")
    telemetry = create_telemetry_provider(cfg, domains=(domain.telemetry_domain,), catalog=catalog)
    baseline_provider = _baseline_provider(cfg)
    legacy_fallback = bool(cfg.get("baselines", {}).get("legacy_fallback", True))
    card = agent_card(catalog, domain_id)
    app.state.telemetry = telemetry
    app.state.domain_id = domain_id

    @app.get("/.well-known/agent.json")
    async def get_card():
        return card

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        task_id, context_id, data = extract_request_context(payload)
        skill = data["skill"]
        incident_for_log = data.get("incident_id") or data.get("payload", {}).get("incident_id", "")
        logger.info("Received request skill=%s incident=%s", skill, incident_for_log)

        if skill == domain.agent.skill.id:
            query = query_from_request({**data, "include_baseline": domain.include_baseline or data.get("include_baseline", False)})
            response = await telemetry.fetch(domain.telemetry_domain, query)
            finding = analyze_domain(
                catalog,
                domain_id,
                response,
                region=data["region"],
                incident_description=str(data.get("incident_description", "")),
                baseline_lookup=baseline_provider.get_baseline,
                legacy_fallback=legacy_fallback,
            )
            logger.info(
                "Analyzed %s region=%s window=%s..%s scenario=%s signals=%s anomaly=%s confidence=%.2f "
                "data_degraded=%s unclassified=%s",
                domain_id, data["region"], query.start_time.isoformat(), query.end_time.isoformat(),
                data.get("scenario_id"), len(response.signals), finding.anomaly_detected, finding.confidence,
                finding.data_degraded, len(finding.unclassified_evidence),
            )
            return build_task_result(
                payload=payload,
                task_id=task_id,
                context_id=context_id,
                state="completed",
                artifact_name="agent_finding",
                data=finding.model_dump(mode="json"),
            )

        # Peer finding broadcasts (respond-to-peer) are acknowledged only.
        # Cross-agent reconsideration is applied by the orchestrator from
        # correlation.json (detection.reconsider), not via this endpoint.
        logger.info("Acknowledged peer message skill=%s incident=%s", skill, incident_for_log)
        return build_task_result(
            payload=payload,
            task_id=task_id,
            context_id=context_id,
            state="completed",
            artifact_name="peer_response",
            data={"ack": True},
        )

    return app


def build_all_domain_apps(cfg: dict | None = None, catalog: Catalog | None = None) -> dict[str, FastAPI]:
    catalog = catalog or get_catalog()
    return {domain_id: build_domain_app(domain_id, cfg, catalog) for domain_id in catalog.domain_ids}
