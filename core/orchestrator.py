from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from agents import config_agent, log_agent, metrics_agent, routing_agent
from agents.rca_synthesizer import synthesize_report
from agents.supervisor import classify_degradation, select_active_agents
from communication.router_base import RouterBase
from models.schemas import A2AMessage, AgentFinding, IncidentRequest, RCAReport


logger = logging.getLogger("net_cortex.orchestrator")


_RECONSIDER_FN_BY_AGENT: dict[str, Any] = {
    "metrics": getattr(metrics_agent, "reconsider_finding", None),
    "log": getattr(log_agent, "reconsider_finding", None),
    "routing": getattr(routing_agent, "reconsider_finding", None),
    "config": getattr(config_agent, "reconsider_finding", None),
}


def apply_local_reconsideration(finding: AgentFinding, peer_findings: list[AgentFinding]) -> AgentFinding:
    fn = _RECONSIDER_FN_BY_AGENT.get(finding.agent_id)
    if callable(fn):
        return fn(finding, peer_findings)
    return finding.model_copy(deep=True)


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


class NetCortexEngine:
    def __init__(self, cfg: dict[str, Any], router: RouterBase):
        self.cfg = cfg
        self.router = router
        self.last_result_state: dict[str, Any] | None = None
        validate_config(cfg)
        self.graph = self._build_graph().compile()

    @staticmethod
    def should_short_circuit(findings: list[AgentFinding]) -> bool:
        return len(findings) > 0 and all(not f.anomaly_detected for f in findings)

    def _build_graph(self) -> StateGraph:
        graph = StateGraph(NetCortexState)

        async def supervisor_node(state: NetCortexState):
            incident = state["incident"]
            llm_model = self.cfg.get("llm", {}).get("model", "gemini-2.5-flash")
            llm_required = bool(self.cfg.get("llm", {}).get("require_success", False))
            dtype = classify_degradation(incident, llm_model=llm_model, require_llm=llm_required)
            active = select_active_agents(dtype)
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

            agent_order: list[str] = []
            task_list: list[asyncio.Task[AgentFinding]] = []
            for agent in state["active_agents"]:
                skill = {
                    "metrics": "analyze-metrics",
                    "log": "analyze-logs",
                    "routing": "analyze-routing",
                    "config": "analyze-config",
                }[agent]
                coro = self.router.send_analysis(
                    agent,
                    incident.incident_id,
                    skill,
                    {
                        "region": incident.region,
                        "window_minutes": int(self.cfg["simulation"]["window_minutes"]),
                        "incident_id": incident.incident_id,
                        "scenario_id": incident.scenario_id,
                    },
                )
                task = asyncio.create_task(asyncio.wait_for(coro, timeout=timeout))
                agent_order.append(agent)
                task_list.append(task)

            findings: list[AgentFinding] = []
            timed_out: list[str] = []
            results = await asyncio.gather(*task_list, return_exceptions=True)
            for agent, result in zip(agent_order, results):
                try:
                    if isinstance(result, Exception):
                        raise result
                    finding = result
                    findings.append(finding)
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
                except asyncio.TimeoutError:
                    timed_out.append(agent)
                    logger.warning("Analysis timeout incident=%s agent=%s", incident.incident_id, agent)
                except Exception:
                    timed_out.append(agent)
                    logger.exception("Analysis error incident=%s agent=%s", incident.incident_id, agent)

            logger.info(
                "Analysis finished incident=%s findings=%s timed_out=%s",
                incident.incident_id,
                len(findings),
                len(timed_out),
            )

            return {"findings": findings, "timed_out_agents": timed_out}

        async def collaboration_node(state: NetCortexState):
            findings = list(state["findings"])
            all_messages: list[A2AMessage] = []
            max_iterations = int(self.cfg["a2a"]["max_iterations"])
            logger.info(
                "Collaboration started incident=%s findings=%s max_iterations=%s",
                state["incident"].incident_id,
                len(findings),
                max_iterations,
            )

            if self.should_short_circuit(findings):
                logger.info(
                    "Collaboration short-circuited incident=%s reason=no_anomalies",
                    state["incident"].incident_id,
                )
                return {
                    "revised_findings": findings,
                    "collaboration_complete": True,
                    "collaboration_round": 1,
                    "a2a_messages": all_messages,
                }

            previous_summaries = {f.agent_id: f.summary for f in findings}
            for round_number in range(1, max_iterations + 1):
                logger.info("Collaboration round=%s incident=%s", round_number, state["incident"].incident_id)
                round_messages: list[A2AMessage] = []
                for finding in findings:
                    round_messages.extend(
                        await self.router.broadcast(
                            sender=finding.agent_id,
                            message_type="finding_publish",
                            payload={
                                "incident_id": state["incident"].incident_id,
                                "summary": finding.summary,
                                "anomaly": finding.anomaly_detected,
                            },
                            round_number=round_number,
                            session_id=state["incident"].incident_id,
                        )
                    )

                # NOTE: reconsideration is applied locally by the orchestrator after
                # broadcast, not by agents processing live A2A messages. This is a
                # Phase 0 simplification; a full A2A implementation would have each
                # agent apply reconsideration on peer finding_publish receipt.
                revised = []
                for finding in findings:
                    peer_findings = [f for f in findings if f.agent_id != finding.agent_id]
                    new_finding = apply_local_reconsideration(finding, peer_findings)
                    revised.append(new_finding)
                findings = revised
                all_messages.extend(round_messages)
                logger.info(
                    "Collaboration round complete incident=%s round=%s messages=%s",
                    state["incident"].incident_id,
                    round_number,
                    len(round_messages),
                )

                current_summaries = {f.agent_id: f.summary for f in findings}
                if current_summaries == previous_summaries:
                    logger.info(
                        "Collaboration converged incident=%s round=%s",
                        state["incident"].incident_id,
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
                state["incident"].incident_id,
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
            report = synthesize_report(
                state["incident"].incident_id,
                state["revised_findings"],
                state["a2a_messages"],
                incident_description=state["incident"].description,
                llm_model=llm_model,
                require_llm=llm_required,
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
            "rca_report": None,
        }
        result = await self.graph.ainvoke(initial)
        self.last_result_state = result
        logger.info("Workflow finished incident=%s", incident.incident_id)
        return result["rca_report"]
