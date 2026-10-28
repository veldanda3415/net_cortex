from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from agents.rca_synthesizer import synthesize_report
from agents.supervisor import classify_degradation, select_active_agents
from communication.router_base import RouterBase
from contract.catalog_loader import Catalog, get_catalog
from detection import reconsider
from detection.investigate import incident_status, rank_causes
from models.schemas import A2AMessage, AgentFinding, IncidentRequest, RCAReport
from providers.query import query_window


logger = logging.getLogger("net_cortex.orchestrator")


def apply_local_reconsideration(
    finding: AgentFinding, peer_findings: list[AgentFinding], catalog: Catalog | None = None
) -> AgentFinding:
    """Apply correlation.json to one finding given its peers' findings."""
    return reconsider(finding, peer_findings, catalog or get_catalog())


def merge_findings(existing: list[AgentFinding], new: list[AgentFinding]) -> list[AgentFinding]:
    existing_map = {f.agent_id: f for f in existing}
    for finding in new:
        existing_map[finding.agent_id] = finding
    return list(existing_map.values())


def merge_a2a_messages(existing: list[A2AMessage], new: list[A2AMessage]) -> list[A2AMessage]:
    existing_ids = {m.message_id for m in existing}
    return existing + [m for m in new if m.message_id not in existing_ids]


class NetCortexState(TypedDict):
    incident: IncidentRequest
    degradation_type: str
    active_agents: list[str]
    findings: Annotated[list[AgentFinding], merge_findings]
    revised_findings: Annotated[list[AgentFinding], merge_findings]
    a2a_messages: Annotated[list[A2AMessage], merge_a2a_messages]
    collaboration_round: int
    collaboration_complete: bool
    timed_out_agents: list[str]
    errored_agents: list[str]
    rca_report: RCAReport | None


def validate_config(cfg: dict[str, Any]) -> None:
    a2a = cfg["a2a"]
    mt = int(a2a["message_timeout_seconds"])
    rt = int(a2a["round_timeout_seconds"])
    ct = int(a2a["collaboration_timeout_seconds"])
    at = int(a2a["analysis_timeout_seconds"])
    mi = int(a2a["max_iterations"])

    if not (mt < rt):
        raise ValueError("ConfigValidationError: message_timeout_seconds must be less than round_timeout_seconds")
    if not (rt * mi <= ct):
        raise ValueError("ConfigValidationError: round_timeout_seconds * max_iterations must be <= collaboration_timeout_seconds")
    if not (at < ct):
        raise ValueError("ConfigValidationError: analysis_timeout_seconds must be < collaboration_timeout_seconds")

    baselines = cfg.get("baselines", {})
    provider = str(baselines.get("provider", "simulation")).lower()
    if provider == "prometheus":
        # PrometheusBaselineProvider.get_baseline() is an unimplemented stub
        # (raises NotImplementedError on first use) — accepting this value
        # here let it pass startup validation only to silently degrade every
        # baseline lookup to the legacy-threshold fallback at runtime. Reject
        # it until the adapter is actually implemented.
        raise ValueError(
            "ConfigValidationError: baselines.provider='prometheus' is not implemented yet "
            "(PrometheusBaselineProvider is a stub) — use 'simulation' until it's implemented."
        )
    if provider not in ("simulation", "telemetry"):
        raise ValueError("ConfigValidationError: baselines.provider must be 'simulation' or 'telemetry'")

    metrics_z_threshold = float(baselines.get("metrics_z_threshold", 3.0))
    if metrics_z_threshold <= 0:
        raise ValueError("ConfigValidationError: baselines.metrics_z_threshold must be > 0")

    config_z_threshold = float(baselines.get("config_z_threshold", 2.5))
    if config_z_threshold <= 0:
        raise ValueError("ConfigValidationError: baselines.config_z_threshold must be > 0")

    legacy_fallback = baselines.get("legacy_fallback", True)
    if not isinstance(legacy_fallback, bool):
        raise ValueError("ConfigValidationError: baselines.legacy_fallback must be a boolean")


def _slim(message: A2AMessage) -> A2AMessage:
    """What the message log keeps: full findings travel in finding_publish /
    finding_update / validation_response, but the log records only who, what
    and the outcome, so the report doesn't repeat every finding N times."""
    payload = dict(message.payload)
    payload.pop("context", None)
    finding = payload.pop("finding", None)
    if isinstance(finding, dict):
        payload.setdefault("summary", finding.get("summary"))
    response = payload.get("response")
    if isinstance(response, dict):
        slim = {k: v for k, v in response.items() if k not in ("finding", "key_events", "evidence", "entity_status",
                                                                 "unclassified_evidence")}
        if "finding" in response and isinstance(response["finding"], dict):
            slim["evidence"] = response["finding"].get("evidence", [])[:5]
        if "agent_id" in response:  # a finding_update reply is a whole AgentFinding
            slim = {"summary": response.get("summary"), "anomaly_detected": response.get("anomaly_detected"),
                    "confidence": response.get("confidence"), "revision_count": response.get("revision_count")}
        payload["response"] = slim
    return message.model_copy(update={"payload": payload})


class NetCortexEngine:
    def __init__(self, cfg: dict[str, Any], router: RouterBase, catalog: Catalog | None = None,
                 topology_provider: Any | None = None):
        self.cfg = cfg
        self.router = router
        self.last_result_state: dict[str, Any] | None = None
        validate_config(cfg)
        # Loading the catalog here makes an invalid one fail engine startup.
        self.catalog = catalog or get_catalog()
        self._topology_provider = topology_provider
        self.graph = self._build_graph().compile()

    async def _topology(self, incident: IncidentRequest) -> Any | None:
        """Topology for ranking causes; None (no ranking) when unavailable."""
        try:
            if self._topology_provider is None:
                from providers.factory import create_telemetry_provider

                first = next(iter(self.catalog.telemetry_domains()))
                self._topology_provider = create_telemetry_provider(self.cfg, domains=(first,), catalog=self.catalog)
            return await self._topology_provider.topology(incident.region, incident.scenario_id)
        except Exception:
            logger.exception("Topology unavailable incident=%s; causes are not ranked", incident.incident_id)
            return None

    def _registered(self, agent_id: str) -> bool:
        registry = getattr(self.router, "registry", None)
        return registry is None or agent_id in registry.agents

    def _window(self, incident: IncidentRequest) -> tuple[Any, Any]:
        return query_window(incident.reported_at, int(self.cfg["simulation"]["window_minutes"]))

    @staticmethod
    def should_short_circuit(findings: list[AgentFinding]) -> bool:
        return len(findings) > 0 and all(not f.anomaly_detected for f in findings)

    def _build_graph(self) -> StateGraph:
        graph = StateGraph(NetCortexState)

        async def supervisor_node(state: NetCortexState):
            incident = state["incident"]
            llm_model = self.cfg.get("llm", {}).get("model", "gemini-2.5-flash")
            llm_required = bool(self.cfg.get("llm", {}).get("require_success", False))
            # classify_degradation calls out to a synchronous Gemini client
            # (blocking network I/O, plus a blocking time.sleep on retry) —
            # run it off the event loop thread so it can't stall the agent
            # uvicorn servers running as tasks on this same loop (see
            # start_runtime in app/main.py) or the asyncio.wait_for timers
            # in analysis_node below.
            dtype = await asyncio.to_thread(
                classify_degradation, incident, llm_model=llm_model, require_llm=llm_required
            )
            active = select_active_agents(dtype, self.catalog)
            logger.info(
                "Supervisor classified incident=%s degradation_type=%s active_agents=%s llm_required=%s",
                incident.incident_id,
                dtype,
                ",".join(active),
                llm_required,
            )
            return {"degradation_type": dtype, "active_agents": active}

        async def analysis_node(state: NetCortexState):
            incident = state["incident"]
            timeout = int(self.cfg["a2a"]["analysis_timeout_seconds"])
            logger.info(
                "Analysis started incident=%s timeout_s=%s",
                incident.incident_id,
                timeout,
            )

            # Anchor the telemetry window on the alarm time, not on when the
            # pipeline happens to run: end = reported_at, start = end - window.
            window_minutes = int(self.cfg["simulation"]["window_minutes"])
            start_time, end_time = query_window(incident.reported_at, window_minutes)

            agent_order: list[str] = []
            task_list: list[asyncio.Task[AgentFinding]] = []
            for agent in state["active_agents"]:
                skill = self.catalog.domain(agent).agent.skill.id
                coro = self.router.send_analysis(
                    agent,
                    incident.incident_id,
                    skill,
                    {
                        "region": incident.region,
                        "window_minutes": window_minutes,
                        "start_time": start_time.isoformat(),
                        "end_time": end_time.isoformat(),
                        "incident_id": incident.incident_id,
                        "incident_description": incident.description,
                        "scenario_id": incident.scenario_id,
                        "anchor_time": incident.reported_at.isoformat(),
                        "alarm_entities": incident.alarm_entities,
                    },
                )
                task = asyncio.create_task(asyncio.wait_for(coro, timeout=timeout))
                agent_order.append(agent)
                task_list.append(task)

            findings: list[AgentFinding] = []
            timed_out: list[str] = []
            errored: list[str] = []
            results = await asyncio.gather(*task_list, return_exceptions=True)
            for agent, result in zip(agent_order, results):
                try:
                    # BaseException, not Exception: asyncio.gather(return_exceptions=True)
                    # can hand back a CancelledError, which is a BaseException
                    # subclass, not an Exception subclass. The old `isinstance(result,
                    # Exception)` check let a CancelledError object fall through as
                    # `finding`, get appended to `findings`, and only then blow up
                    # reading `finding.domain` on the next line — by which point the
                    # bad object was already in the list, later crashing
                    # merge_findings on `f.agent_id`.
                    if isinstance(result, BaseException):
                        raise result
                    finding = result
                    logger.info(
                        "Analysis result incident=%s agent=%s domain=%s anomaly=%s confidence=%.2f summary=%s key_events=%s",
                        incident.incident_id,
                        agent,
                        finding.domain,
                        finding.anomaly_detected,
                        finding.confidence,
                        finding.summary,
                        len(finding.key_events),
                    )
                    # Appended only after the finding has proven safe to log
                    # (i.e. is a real AgentFinding), so a malformed/exception
                    # result never lands in `findings`.
                    findings.append(finding)
                except asyncio.TimeoutError:
                    timed_out.append(agent)
                    logger.warning("Analysis timeout incident=%s agent=%s", incident.incident_id, agent)
                except BaseException:
                    errored.append(agent)
                    logger.exception("Analysis error incident=%s agent=%s", incident.incident_id, agent)

            logger.info(
                "Analysis finished incident=%s findings=%s timed_out=%s errored=%s",
                incident.incident_id,
                len(findings),
                len(timed_out),
                len(errored),
            )

            return {"findings": findings, "timed_out_agents": timed_out, "errored_agents": errored}

        async def collaboration_node(state: NetCortexState):
            """Round-based, collect-then-revise (net_cortex_impl_v4.md §3):

              1. every agent's finding is published to its peers (finding_publish);
                 once an agent has heard from all of them it replies with the
                 targeted validation_requests it wants to send
              2. each request is routed to the domain it asks; the answer (a
                 scoped re-run of that domain's detection) goes back to the
                 asker as a validation_response, whose leads seed next round
              3. every agent revises its finding once (finding_update)

            Stops when nothing changed and nobody asked anything, or after
            a2a.max_iterations rounds. An agent that only acks gets its
            finding revised here from correlation.json, as before.
            """
            incident = state["incident"]
            findings = list(state["findings"])
            all_messages: list[A2AMessage] = []
            max_iterations = int(self.cfg["a2a"]["max_iterations"])
            session = incident.incident_id
            logger.info(
                "Collaboration started incident=%s findings=%s max_iterations=%s",
                incident.incident_id,
                len(findings),
                max_iterations,
            )

            if self.should_short_circuit(findings):
                logger.info(
                    "Collaboration short-circuited incident=%s reason=no_anomalies",
                    incident.incident_id,
                )
                return {
                    "revised_findings": findings,
                    "collaboration_complete": True,
                    "collaboration_round": 1,
                    "a2a_messages": all_messages,
                }

            start_time, end_time = self._window(incident)
            context = {
                "region": incident.region, "start_time": start_time.isoformat(), "end_time": end_time.isoformat(),
                "scenario_id": incident.scenario_id, "anchor_time": incident.reported_at.isoformat(),
                "alarm_entities": incident.alarm_entities, "incident_description": incident.description,
            }
            asked: set[str] = set()
            previous_summaries = {f.agent_id: f.summary for f in findings}
            for round_number in range(1, max_iterations + 1):
                logger.info("Collaboration round=%s incident=%s", round_number, incident.incident_id)
                round_messages: list[A2AMessage] = []
                participants = [f.agent_id for f in findings]

                # 1. publish; replies carry each agent's questions
                published = await asyncio.gather(*(
                    self.router.broadcast(
                        sender=finding.agent_id,
                        message_type="finding_publish",
                        payload={
                            "incident_id": incident.incident_id,
                            "summary": finding.summary,
                            "anomaly": finding.anomaly_detected,
                            "finding": finding.model_dump(mode="json"),
                            "context": {**context, "asked": sorted(asked)},
                            "round": round_number,
                            "expect_peers": participants,
                        },
                        round_number=round_number,
                        session_id=session,
                    )
                    for finding in findings
                ))
                requests: dict[str, dict[str, Any]] = {}
                for messages in published:
                    round_messages.extend(messages)
                    for message in messages:
                        for request in (message.payload.get("response") or {}).get("requests", []) or []:
                            rid = request.get("request_id")
                            if rid and rid not in asked and rid not in requests and self._registered(str(request.get("target"))):
                                requests[rid] = request
                asked.update(requests)

                # 2. route questions, then deliver each answer to its asker
                if requests:
                    logger.info("Collaboration round=%s incident=%s validation_requests=%s",
                                round_number, incident.incident_id, len(requests))
                    asks = list(requests.values())
                    answered = await asyncio.gather(*(
                        self.router.send_direct(
                            sender=str(r["sender"]), target=str(r["target"]), message_type="validation_request",
                            payload={**r, "incident_id": incident.incident_id},
                            round_number=round_number, session_id=session,
                        )
                        for r in asks
                    ))
                    round_messages.extend(answered)
                    deliveries = []
                    for request, message in zip(asks, answered):
                        result = message.payload.get("response") or {}
                        if not result.get("request_id"):
                            continue
                        logger.info("Validation incident=%s %s -> %s about %s:%s verdict=%s",
                                    incident.incident_id, request["sender"], request["target"],
                                    request["entity"]["type"], request["entity"]["id"], result.get("verdict"))
                        deliveries.append(self.router.send_direct(
                            sender=str(request["target"]), target=str(request["sender"]),
                            message_type="validation_response",
                            payload={"incident_id": incident.incident_id, "request_id": result["request_id"],
                                     "verdict": result.get("verdict"), "entity": result.get("entity"),
                                     "summary": result.get("summary"), "leads": result.get("leads", []),
                                     "evidence": (result.get("finding") or {}).get("evidence", [])[:5]},
                            round_number=round_number, session_id=session,
                        ))
                    round_messages.extend(await asyncio.gather(*deliveries))

                # 3. every agent revises once
                updates = await asyncio.gather(*(
                    self.router.send_direct(
                        sender=finding.agent_id, target=finding.agent_id, message_type="finding_update",
                        payload={"incident_id": incident.incident_id, "round": round_number},
                        round_number=round_number, session_id=session,
                    )
                    for finding in findings
                ))
                round_messages.extend(updates)
                revised = []
                for finding, message in zip(findings, updates):
                    response = message.payload.get("response") or {}
                    if response.get("agent_id") == finding.agent_id:
                        revised.append(AgentFinding.model_validate(response))
                        continue
                    # NOTE: an agent that only acks (older or external) is revised
                    # here from correlation.json, as in Phase 0.
                    peer_findings = [f for f in findings if f.agent_id != finding.agent_id]
                    revised.append(apply_local_reconsideration(finding, peer_findings, self.catalog))
                findings = revised
                all_messages.extend(_slim(m) for m in round_messages)
                logger.info(
                    "Collaboration round complete incident=%s round=%s messages=%s",
                    incident.incident_id,
                    round_number,
                    len(round_messages),
                )

                current_summaries = {f.agent_id: f.summary for f in findings}
                if current_summaries == previous_summaries and not requests:
                    logger.info(
                        "Collaboration converged incident=%s round=%s",
                        incident.incident_id,
                        round_number,
                    )
                    return {
                        "revised_findings": findings,
                        "collaboration_complete": True,
                        "collaboration_round": round_number,
                        "a2a_messages": all_messages,
                    }
                previous_summaries = current_summaries

            logger.info(
                "Collaboration reached max iterations incident=%s iterations=%s",
                incident.incident_id,
                max_iterations,
            )
            return {
                "revised_findings": findings,
                "collaboration_complete": True,
                "collaboration_round": max_iterations,
                "a2a_messages": all_messages,
            }

        async def synthesizer_node(state: NetCortexState):
            llm_model = self.cfg.get("llm", {}).get("model", "gemini-2.5-flash")
            llm_required = bool(self.cfg.get("llm", {}).get("require_success", False))
            logger.info(
                "Synthesizer started incident=%s llm_model=%s findings=%s",
                state["incident"].incident_id,
                llm_model,
                len(state["revised_findings"]),
            )
            # synthesize_report calls out to a synchronous Gemini client
            # (blocking network I/O, plus a blocking time.sleep on retry) —
            # see the identical rationale on classify_degradation above.
            findings = state["revised_findings"]
            incident = state["incident"]
            ranked = []
            if any(f.anomaly_detected for f in findings):
                topology = await self._topology(incident)
                ranked = rank_causes(findings, topology, self.catalog, incident.alarm_entities)
            report = await asyncio.to_thread(
                synthesize_report,
                state["incident"].incident_id,
                state["revised_findings"],
                state["a2a_messages"],
                incident_description=state["incident"].description,
                llm_model=llm_model,
                require_llm=llm_required,
                total_agents=len(state["active_agents"]),
                timed_out_agents=state["timed_out_agents"],
                errored_agents=state["errored_agents"],
                catalog=self.catalog,
                ranked_causes=ranked,
                alarm_entities=incident.alarm_entities,
                status=incident_status(findings),
            )
            logger.info(
                "Synthesizer finished incident=%s confidence=%.2f",
                state["incident"].incident_id,
                report.confidence_score,
            )
            return {"rca_report": report}

        graph.add_node("supervisor", supervisor_node)
        graph.add_node("analysis", analysis_node)
        graph.add_node("collaboration", collaboration_node)
        graph.add_node("synthesizer", synthesizer_node)

        graph.set_entry_point("supervisor")
        graph.add_edge("supervisor", "analysis")
        graph.add_edge("analysis", "collaboration")
        graph.add_edge("collaboration", "synthesizer")
        graph.add_edge("synthesizer", END)

        return graph

    async def run_incident(self, incident: IncidentRequest) -> RCAReport:
        logger.info("Workflow started incident=%s", incident.incident_id)
        initial: NetCortexState = {
            "incident": incident,
            "degradation_type": "unknown",
            "active_agents": [],
            "findings": [],
            "revised_findings": [],
            "a2a_messages": [],
            "collaboration_round": 0,
            "collaboration_complete": False,
            "timed_out_agents": [],
            "errored_agents": [],
            "rca_report": None,
        }
        result = await self.graph.ainvoke(initial)
        self.last_result_state = result
        logger.info("Workflow finished incident=%s", incident.incident_id)
        return result["rca_report"]
