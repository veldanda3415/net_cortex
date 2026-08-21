from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI

from agents.a2a_protocol import build_task_result, extract_request_context
from models.schemas import AgentFinding
from providers.adapters.prometheus_baseline_adapter import PrometheusBaselineProvider
from providers.baseline_utils import compute_z_score, is_anomalous
from providers.simulation.baseline_sim import SimulationBaselineProvider
from providers.factory import create_config_provider


logger = logging.getLogger("net_cortex.agent.config")


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "have", "has", "had",
    "into", "onto", "about", "after", "before", "during", "high", "low", "drop", "spike", "region",
    "incident", "service", "network",
}


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
        if len(token) >= 4 and token not in _STOPWORDS
    }


def _is_change_relevant(change, incident_description: str) -> bool:
    if not incident_description.strip():
        return True

    incident_tokens = _tokenize(incident_description)
    if not incident_tokens:
        return True

    change_blob = (
        f"{change.component} {change.change_type} "
        f"{' '.join(str(k) for k in change.before.keys())} {' '.join(str(v) for v in change.before.values())} "
        f"{' '.join(str(k) for k in change.after.keys())} {' '.join(str(v) for v in change.after.values())}"
    )
    change_tokens = _tokenize(change_blob)

    if incident_tokens & change_tokens:
        return True

    # Network symptom incidents are often caused by policy/bandwidth config changes
    # even when the textual overlap is weak.
    has_network_symptom = any(k in incident_description.lower() for k in ("throughput", "latency", "packet", "error", "loss"))
    if has_network_symptom and change.change_type in {"policy_update", "bandwidth_limit", "rollback"}:
        return True

    return False


def reconsider_finding(finding: AgentFinding, peer_findings: list[AgentFinding]) -> AgentFinding:
    """Adjust config confidence/summary using peer domain evidence."""
    revised = finding.model_copy(deep=True)

    peer_anomalies = [peer for peer in peer_findings if peer.anomaly_detected]
    if revised.anomaly_detected:
        # Idempotency guard: see metrics_agent.reconsider_finding for why this
        # must gate the whole block, not just a summary text append. This
        # branch previously had no summary marker at all, so confidence kept
        # climbing every collaboration round even though the orchestrator's
        # convergence check (summary equality) saw no change.
        if peer_anomalies and "Peer corroboration:" not in revised.summary:
            revised.revised = True
            revised.revision_count += 1
            revised.confidence = min(0.95, revised.confidence + 0.04)
            domains = ", ".join(sorted({p.domain for p in peer_anomalies}))
            revised.summary = (
                f"{revised.summary}. "
                f"Peer corroboration: aligned anomalies from [{domains}]"
            )
        return revised

    # No config change found while peers report anomalies is valuable contradictory evidence.
    if peer_anomalies and "Peer contradiction:" not in revised.summary:
        revised.revised = True
        revised.revision_count += 1
        revised.confidence = max(revised.confidence, 0.86)
        domains = ", ".join(sorted({p.domain for p in peer_anomalies}))
        revised.summary = (
            f"{revised.summary}. "
            f"Peer contradiction: anomalies observed in [{domains}] while config remains unchanged"
        )

    return revised


def build_config_app(cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="netcortex-config-agent")
    provider = create_config_provider(cfg or {})
    baseline_cfg = (cfg or {}).get("baselines", {})
    baseline_provider_name = str(baseline_cfg.get("provider", "simulation")).lower()
    if baseline_provider_name == "simulation":
        baseline_provider = SimulationBaselineProvider()
    elif baseline_provider_name == "prometheus":
        baseline_provider = PrometheusBaselineProvider()
    else:
        raise ValueError("ConfigValidationError: baselines.provider must be either 'simulation' or 'prometheus'")
    z_threshold = float(baseline_cfg.get("config_z_threshold", 2.5))
    legacy_fallback = bool(baseline_cfg.get("legacy_fallback", True))

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-config-agent",
            "version": "1.0.0",
            "description": "Analyzes configuration changes proximate to incident windows.",
            "url": "http://localhost:8004/a2a",
            "endpoint": "http://localhost:8004/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "analyze-config",
                    "name": "Analyze Config",
                    "description": "Analyze config changes in a time window and produce an AgentFinding.",
                    "tags": ["config", "changes", "rca"],
                },
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

    @app.post("/a2a")
    async def tasks_send(payload: dict):
        task_id, context_id, data = extract_request_context(payload)
        skill = data["skill"]
        incident_for_log = data.get("incident_id") or data.get("payload", {}).get("incident_id", "")
        logger.info("Received request skill=%s incident=%s", skill, incident_for_log)

        if skill == "analyze-config":
            changes = provider.get_config_changes(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
            data_degraded = getattr(provider, "degraded", False)
            incident_description = str(data.get("incident_description", ""))
            relevant_changes = [c for c in changes if _is_change_relevant(c, incident_description)]
            component_counts: dict[str, int] = defaultdict(int)
            for change in relevant_changes:
                component_counts[change.component] += 1

            anomalous_components: list[tuple[str, float]] = []
            baseline_hits = 0
            for component, count in component_counts.items():
                baseline = baseline_provider.get_baseline(f"component:{component}", "change_count")
                if baseline is None:
                    baseline = baseline_provider.get_baseline(f"region:{data['region']}", "change_count")
                if baseline is None:
                    continue
                baseline_hits += 1
                z_score = compute_z_score(float(count), baseline)
                if is_anomalous(float(count), baseline, z_threshold=z_threshold):
                    anomalous_components.append((component, z_score))

            # Primary responsibility: if incident-window relevant changes exist, surface them directly.
            if len(relevant_changes) > 0:
                anomaly = True
                latest = max(relevant_changes, key=lambda c: c.timestamp)
                summary = (
                    f"Incident-relevant config changes found: {len(relevant_changes)} change(s); "
                    f"latest={latest.change_type} on {latest.component}"
                )
                if anomalous_components:
                    top_component, top_z = max(anomalous_components, key=lambda item: item[1])
                    summary = (
                        f"{summary}; volume anomaly on {top_component} z={top_z:.2f}"
                    )
            else:
                if baseline_hits == 0:
                    anomaly = False if not legacy_fallback else False
                else:
                    anomaly = bool(anomalous_components)

                summary = "No incident-relevant config changes"
                if len(changes) > 0 and len(relevant_changes) == 0:
                    summary = f"Config changes present ({len(changes)}) but not incident-relevant"
                if anomaly and anomalous_components:
                    top_component, top_z = max(anomalous_components, key=lambda item: item[1])
                    summary = (
                        f"Config change volume anomaly detected without direct change records; "
                        f"top_component={top_component} z={top_z:.2f}"
                    )
            logger.info(
                "Analyzed config region=%s window=%s scenario=%s changes=%s relevant_changes=%s anomaly=%s data_degraded=%s",
                data["region"],
                data["window_minutes"],
                data.get("scenario_id"),
                len(changes),
                len(relevant_changes),
                anomaly,
                data_degraded,
            )
            # Prefer relevant_changes for the finding's time range; when empty
            # (the anomaly-without-direct-changes branch) fall back to the
            # full `changes` list's real timestamps rather than synthesizing
            # start_time == end_time == now, which corrupted downstream
            # chronological ordering in synthesis.
            time_source = relevant_changes if relevant_changes else changes
            finding = AgentFinding(
                agent_id="config",
                domain="config",
                anomaly_detected=anomaly,
                summary=summary,
                key_events=[c.model_dump() for c in relevant_changes[:5]],
                start_time=min((c.timestamp for c in time_source), default=datetime.now(timezone.utc)),
                end_time=max((c.timestamp for c in time_source), default=datetime.now(timezone.utc)),
                confidence=(
                    min(0.95, 0.75 + 0.1 * max((z for _, z in anomalous_components), default=0.0))
                    if len(relevant_changes) > 0
                    else (min(0.9, 0.6 + 0.1 * max((z for _, z in anomalous_components), default=0.0)) if anomaly else 0.2)
                ),
                data_degraded=data_degraded,
            )
            logger.info("Completed analyze-config anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
            return build_task_result(
                payload=payload,
                task_id=task_id,
                context_id=context_id,
                state="completed",
                artifact_name="agent_finding",
                data=finding.model_dump(mode="json"),
            )

        # Peer finding broadcasts (respond-to-peer) are acknowledged only.
        # Real cross-agent reconsideration is applied locally by the
        # orchestrator (apply_local_reconsideration in
        # core/orchestrator.py's collaboration_node), not via this endpoint.
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
