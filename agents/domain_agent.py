"""Generic domain agent, instantiated per domain from the detection catalog.

One implementation serves every domain in contract/catalog/domains.json:
fetch the domain's telemetry over MCP, evaluate catalog rules
(detection.analyze_domain), return an AgentFinding over A2A. The agent
card (name, port, skill) also comes from the catalog, so the built-in
metrics/log/routing/config agents keep their existing identities and a
new domain needs no code.

Collaboration (`respond-to-peer`, one round = collect, then revise once):

  finding_publish     a peer's finding. Once every expected peer has
                      published this round, the reply carries the
                      validation_requests this agent wants to send
                      (detection.investigate.plan_requests over topology).
  validation_request  a peer's question about one entity and time window.
                      Answered by re-running this domain's detection on a
                      query scoped to that entity (entity_filters).
  validation_response the answer to one of this agent's questions; its
                      leads seed the next round's questions.
  finding_update      end of round: revise this agent's finding once, with
                      the evidence its answers turned up and correlation.json.

With the LLM on (agents/triage.py), an agent holding unclassified evidence
it has not shown the LLM yet (its own, or what its scoped answers turned up)
also asks the LLM, in plan(), for hypotheses: temporary rules over that evidence. Each one is a validation_request to this agent,
and answer() accepts it only if the code checks in detection/hypotheses.py
pass; a supported one joins the finding as evidence via="hypothesis".
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI

from agents import triage
from agents.a2a_protocol import build_task_result, extract_request_context
from contract import TelemetryQuery
from contract.catalog_loader import Catalog, get_catalog
from detection import analyze_domain, reconsider
from detection import hypotheses
from detection.investigate import absorb, find_leads, plan_requests, verdict
from models.schemas import AgentFinding
from providers.adapters.prometheus_baseline_adapter import PrometheusBaselineProvider
from providers.base import BaselineProvider
from providers.factory import create_telemetry_provider
from providers.query import query_from_request
from providers.simulation.baseline_sim import SimulationBaselineProvider

MAX_SESSIONS = 256
# Is it still happening? Read the minutes after the alarm, when the alarm is
# at least MIN_STATUS_WINDOW old, up to MAX_STATUS_WINDOW after it.
MIN_STATUS_WINDOW = timedelta(seconds=60)
MAX_STATUS_WINDOW = timedelta(minutes=15)


class _NoBaselines(BaselineProvider):
    """baselines.provider: telemetry. Z-scores come from the baseline window
    the telemetry source returns with the data (include_baseline), not from a
    separate store; without enough baseline samples the legacy fallback applies."""

    def get_baseline(self, entity_key: str, metric: str):
        return None


def _baseline_provider(cfg: dict[str, Any]) -> BaselineProvider:
    name = str(cfg.get("baselines", {}).get("provider", "simulation")).lower()
    if name == "simulation":
        return SimulationBaselineProvider()
    if name == "telemetry":
        return _NoBaselines()
    if name == "prometheus":
        return PrometheusBaselineProvider()
    raise ValueError("ConfigValidationError: baselines.provider must be 'simulation' or 'telemetry'")


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
                "description": "Collaboration rounds: read peers' findings and ask them targeted, topology-scoped "
                "questions (validation_request); answer peers' questions with this domain's detection on a "
                "scoped query; revise this agent's finding once per round (finding_update).",
                "tags": ["a2a", "collaboration"],
            },
        ],
        "schemaContract": {"outputSchema": "AgentFinding", "version": "1.0.0"},
    }


@dataclass
class _Session:
    context: dict[str, Any] = field(default_factory=dict)
    own: AgentFinding | None = None
    peers: dict[int, dict[str, AgentFinding]] = field(default_factory=dict)  # round -> sender -> finding
    answers: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = field(default_factory=dict)
    leads: list[dict[str, Any]] = field(default_factory=list)
    planned: set[int] = field(default_factory=set)
    # Unclassified rows the LLM triage has not read yet: this agent's own, then
    # what its scoped answers to peers turned up (possibly older than the window).
    material: list[dict[str, Any]] = field(default_factory=list)
    triaged: set[str] = field(default_factory=set)


class _Sessions:
    def __init__(self, limit: int = MAX_SESSIONS) -> None:
        self._items: OrderedDict[str, _Session] = OrderedDict()
        self._limit = limit

    def get(self, key: str) -> _Session:
        session = self._items.pop(key, None) or _Session()
        self._items[key] = session
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        return session


def _parse_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def build_domain_app(domain_id: str, cfg: dict | None = None, catalog: Catalog | None = None) -> FastAPI:
    cfg = cfg or {}
    catalog = catalog or get_catalog()
    domain = catalog.domain(domain_id)  # KeyError -> unknown domain, fail at startup
    logger = logging.getLogger(f"net_cortex.agent.{domain_id}")

    app = FastAPI(title=f"netcortex-{domain_id}-agent")
    telemetry = create_telemetry_provider(cfg, domains=(domain.telemetry_domain,), catalog=catalog)
    baseline_provider = _baseline_provider(cfg)
    legacy_fallback = bool(cfg.get("baselines", {}).get("legacy_fallback", True))
    include_baseline = domain.include_baseline or domain_id in (cfg.get("telemetry", {}) or {}).get("include_baseline", [])
    card = agent_card(catalog, domain_id)
    sessions = _Sessions()
    app.state.telemetry = telemetry
    app.state.domain_id = domain_id
    app.state.sessions = sessions

    async def analyze(query: TelemetryQuery, context: dict[str, Any], *, scoped: bool = False) -> AgentFinding:
        response = await telemetry.fetch(domain.telemetry_domain, query)
        after = []
        now = datetime.now(timezone.utc)
        if not scoped and now - query.end_time >= MIN_STATUS_WINDOW:
            later = query.model_copy(update={"start_time": query.end_time, "include_baseline": False,
                                             "end_time": min(now, query.end_time + MAX_STATUS_WINDOW)})
            after = (await telemetry.fetch(domain.telemetry_domain, later)).signals
        finding = analyze_domain(
            catalog,
            domain_id,
            response,
            region=query.region,
            incident_description=str(context.get("incident_description", "")),
            baseline_lookup=baseline_provider.get_baseline,
            legacy_fallback=legacy_fallback,
            scoped=scoped,
            after=after,
        )
        logger.info(
            "Analyzed %s region=%s window=%s..%s scenario=%s signals=%s anomaly=%s confidence=%.2f "
            "data_degraded=%s unclassified=%s%s",
            domain_id, query.region, query.start_time.isoformat(), query.end_time.isoformat(),
            context.get("scenario_id"), len(response.signals), finding.anomaly_detected, finding.confidence,
            finding.data_degraded, len(finding.unclassified_evidence),
            f" scoped_to={[f.id for f in query.entity_filters or []]}" if scoped else "",
        )
        return finding

    async def plan(session: _Session, round_number: int) -> list[dict[str, Any]]:
        ctx = session.context
        peers = list(session.peers.get(round_number, {}).values())
        try:
            topology = await telemetry.topology(ctx["region"], ctx.get("scenario_id"))
        except Exception:
            logger.exception("Topology unavailable; no questions this round")
            return []
        requests = plan_requests(
            domain_id, session.own, peers, topology, catalog,
            window_start=_parse_time(ctx["start_time"]), window_end=_parse_time(ctx["end_time"]),
            alarm_entities=ctx.get("alarm_entities") or [], leads=session.leads,
            asked=ctx.get("asked", []),
        )
        session.leads = []
        for r in requests:
            r["sender"] = domain_id
            logger.info("Asking %s about %s:%s round=%s (%s)", r["target"], r["entity"]["type"], r["entity"]["id"],
                        round_number, r["question"])
        return await triage_requests(session, peers, topology) + requests

    async def triage_requests(session: _Session, peers: list[AgentFinding], topology: Any) -> list[dict[str, Any]]:
        """The LLM's hypotheses over the unclassified rows it has not read yet."""
        own = session.own
        if own is None or not triage.llm_enabled(cfg):
            return []
        fresh = []
        for row in [*own.unclassified_evidence, *session.material]:
            key = row.get("evidence_ref") or ""
            if key and key not in session.triaged:
                session.triaged.add(key)
                fresh.append(row)
        session.material = []
        if not fresh:
            return []
        view = own.model_copy(update={"unclassified_evidence": fresh})
        prompt = triage.build_prompt(domain_id, view, peers, session.context)
        raw = await asyncio.to_thread(triage._call, prompt, str(cfg.get("llm", {}).get("model", "gemini-2.5-flash")))
        out = triage.to_requests(raw, domain_id, view, session.context, topology, catalog)
        for r in out:
            logger.info("Hypothesis %s: %s", r["hypothesis"]["rule_id"], r["question"])
        return out

    async def check_hypothesis(request: dict[str, Any], session: _Session) -> dict[str, Any]:
        """The four code checks (detection/hypotheses.py) on fresh, scoped reads."""
        ctx = session.context
        h = request["hypothesis"]
        rule = hypotheses.parse_rule(h["rule"])
        aliases = {(f["type"], f["id"]) for f in request["entity_filters"]}
        start, end = _parse_time(request["start_time"]), _parse_time(request["end_time"])

        async def read(a: datetime, b: datetime):
            query = query_from_request({
                "region": ctx["region"], "start_time": a.isoformat(), "end_time": b.isoformat(),
                "entity_filters": request["entity_filters"], "scenario_id": ctx.get("scenario_id"),
                "anchor_time": ctx.get("anchor_time"), "include_baseline": False,
            })
            return await telemetry.fetch(domain.telemetry_domain, query)

        now = await read(start, end)
        hits = hypotheses.fire(rule, h["rule_id"], h["signal"], now.signals, aliases, ctx["region"])
        refs = {s.evidence_ref for s in now.signals if s.name == h["signal"] and (s.entity.type, s.entity.id) in aliases}
        checks = {"grounded": bool(h["evidence_refs"]) and set(h["evidence_refs"]) <= refs, "fires": bool(hits)}
        before = hypotheses.window_before(start, end, catalog.investigation.walk.lookback_minutes)
        earlier = await read(*before)
        checks["new"] = not hypotheses.fire(rule, h["rule_id"], h["signal"], earlier.signals, aliases, ctx["region"])
        try:
            topology = await telemetry.topology(ctx["region"], ctx.get("scenario_id"))
        except Exception:
            logger.exception("Topology unavailable; hypothesis %s cannot be placed", h["rule_id"])
            topology = None
        findings = [f for f in [session.own, *session.peers.get(max(session.peers, default=0), {}).values()] if f]
        checks["placed"] = hypotheses.placed(h["entity"], topology, catalog, ctx.get("alarm_entities") or [], findings)
        degraded = now.degraded or earlier.degraded
        ok = all(checks.values())
        result_verdict = "supported" if ok else ("inconclusive" if degraded else "refuted")
        failed = [c for c in hypotheses.CHECKS if not checks[c]]
        summary = (f"hypothesis {h['rule_id']} on {h['entity']['type']}:{h['entity']['id']}: "
                   + ("all checks passed" if ok else f"failed {', '.join(failed)}"))
        finding = None
        if ok:
            h["example_text"] = h.get("example_text") or [t for t in (hypotheses.signal_text(s) for s, _ in hits[:3]) if t]
            h["classify_pattern"] = hypotheses.verify_pattern(h.get("classify_pattern"), h["example_text"])
            assert session.own is not None
            finding = session.own.model_copy(update={
                "anomaly_detected": True, "summary": f"{domain_id}: verified hypothesis {h['rule_id']}: {h['why']}",
                "key_events": [hypotheses.key_event(h, hits)], "evidence": [hypotheses.evidence_row(h, hits)],
                "start_time": min(s.timestamp for s, _ in hits), "end_time": max(s.timestamp for s, _ in hits),
                "confidence": max(domain.confidence.anomaly.base, 0.5),
            }).model_dump(mode="json")
        logger.info("Checked %s: %s %s", h["rule_id"], result_verdict, checks)
        return {"request_id": request["request_id"], "verdict": result_verdict, "entity": request["entity"],
                "summary": summary, "checks": checks, "hypothesis": h, "leads": [],
                **({"finding": finding} if finding else {})}

    async def answer(request: dict[str, Any], session: _Session, round_number: int) -> dict[str, Any]:
        ctx = session.context
        if isinstance(request.get("hypothesis"), dict):
            result = await check_hypothesis(request, session)
            if result["verdict"] == "supported":
                session.answers.setdefault(round_number, []).append((request, result))
            return result
        query = query_from_request({
            "region": ctx["region"], "start_time": request["start_time"], "end_time": request["end_time"],
            "entity_filters": request["entity_filters"], "scenario_id": ctx.get("scenario_id"),
            "anchor_time": ctx.get("anchor_time"),
            "include_baseline": include_baseline,
        })
        scoped = await analyze(query, ctx, scoped=True)
        if triage.llm_enabled(cfg):
            session.material.extend(scoped.unclassified_evidence)
        asked = {(f["type"], f["id"]) for f in request["entity_filters"]}
        result = {
            "request_id": request["request_id"],
            "verdict": verdict(scoped),
            "entity": request["entity"],
            "summary": scoped.summary,
            "finding": scoped.model_dump(mode="json"),
            "leads": find_leads(scoped, catalog, exclude=asked),
        }
        if result["verdict"] == "supported":
            session.answers.setdefault(round_number, []).append((request, result))
        logger.info("Answered %s about %s:%s verdict=%s leads=%s", request.get("sender"), request["entity"]["type"],
                    request["entity"]["id"], result["verdict"], len(result["leads"]))
        return result

    def revise(session: _Session, round_number: int) -> AgentFinding | None:
        if session.own is None:
            return None
        peers = list(session.peers.get(round_number, {}).values())
        revised = absorb(session.own, session.answers.get(round_number, []), catalog, limit=domain.key_events.limit)
        revised = reconsider(revised, peers, catalog)
        session.own = revised
        return revised

    @app.get("/.well-known/agent.json")
    async def get_card():
        return card

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        task_id, context_id, data = extract_request_context(payload)
        skill = data["skill"]
        incident_for_log = data.get("incident_id") or data.get("payload", {}).get("incident_id", "")
        logger.info("Received request skill=%s incident=%s", skill, incident_for_log)

        def reply(artifact_name: str, body: dict[str, Any]) -> dict:
            return build_task_result(payload=payload, task_id=task_id, context_id=context_id, state="completed",
                                     artifact_name=artifact_name, data=body)

        if skill == domain.agent.skill.id:
            query = query_from_request({**data, "include_baseline": include_baseline or data.get("include_baseline", False)})
            finding = await analyze(query, data)
            session = sessions.get(str(data.get("incident_id") or context_id))
            session.context = {
                "region": data["region"], "start_time": query.start_time.isoformat(),
                "end_time": query.end_time.isoformat(), "scenario_id": data.get("scenario_id"),
                "anchor_time": data.get("anchor_time"), "alarm_entities": data.get("alarm_entities") or [],
                "incident_description": data.get("incident_description", ""),
            }
            session.own = finding
            return reply("agent_finding", finding.model_dump(mode="json"))

        message_type = data.get("message_type")
        body = data.get("payload") or {}
        round_number = int(data.get("round_number") or body.get("round") or 0)
        session = sessions.get(str(context_id or body.get("incident_id", "")))
        if body.get("context"):
            session.context = {**session.context, **body["context"]}

        if message_type == "finding_publish" and isinstance(body.get("finding"), dict):
            sender = str(data.get("sender_agent"))
            session.peers.setdefault(round_number, {})[sender] = AgentFinding.model_validate(body["finding"])
            expected = set(body.get("expect_peers") or []) - {domain_id}
            if expected and expected <= set(session.peers[round_number]) and round_number not in session.planned \
                    and session.context.get("region"):
                session.planned.add(round_number)
                return reply("peer_response", {"ack": True, "requests": await plan(session, round_number)})
            return reply("peer_response", {"ack": True})

        if message_type == "validation_request" and session.context.get("region"):
            return reply("validation_response", await answer(body, session, round_number))

        if message_type == "validation_response":
            session.leads.extend(body.get("leads") or [])
            return reply("peer_response", {"ack": True})

        if message_type == "finding_update":
            revised = revise(session, round_number)
            if revised is not None:
                return reply("agent_finding", revised.model_dump(mode="json"))

        # Anything else (an old-style broadcast, or a message for a session this
        # agent no longer holds) is acknowledged; the orchestrator then revises
        # this agent's finding itself from correlation.json.
        logger.info("Acknowledged peer message skill=%s type=%s incident=%s", skill, message_type, incident_for_log)
        return reply("peer_response", {"ack": True})

    return app


def build_all_domain_apps(cfg: dict | None = None, catalog: Catalog | None = None) -> dict[str, FastAPI]:
    catalog = catalog or get_catalog()
    return {domain_id: build_domain_app(domain_id, cfg, catalog) for domain_id in catalog.domain_ids}
