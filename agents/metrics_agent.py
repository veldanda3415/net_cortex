from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI

from agents.a2a_protocol import build_task_result, extract_request_context
from models.schemas import AgentFinding
from providers.adapters.prometheus_baseline_adapter import PrometheusBaselineProvider
from providers.baseline_utils import compute_z_score, is_anomalous
from providers.simulation.baseline_sim import SimulationBaselineProvider
from providers.factory import create_metrics_provider


logger = logging.getLogger("net_cortex.agent.metrics")


def _throughput_below(event: dict, threshold: float = 0.7) -> bool:
    try:
        return float(event.get("throughput_gbps", 999)) < threshold
    except (TypeError, ValueError):
        return False


def _metric_entity_keys(metric_row: dict) -> list[str]:
    tags = metric_row.get("tags", {}) if isinstance(metric_row.get("tags", {}), dict) else {}
    keys: list[str] = []
    for tag_name in ("switch", "interface", "uplink", "dst_prefix", "core", "lag", "service"):
        tag_value = tags.get(tag_name)
        if tag_value:
            keys.append(f"{tag_name}:{tag_value}")
    region = metric_row.get("region")
    if region:
        keys.append(f"region:{region}")
    return keys


def reconsider_finding(finding: AgentFinding, peer_findings: list[AgentFinding]) -> AgentFinding:
    """Adjust metrics confidence/summary using peer domain evidence."""
    revised = finding.model_copy(deep=True)
    if not revised.anomaly_detected:
        return revised

    # Idempotency guard: once this note is in the summary, the corroboration
    # has already been applied — re-running it on a later collaboration round
    # (whose only change might be a *different* peer) must not keep bumping
    # confidence/revision_count while the orchestrator's convergence check
    # only compares summaries.
    if "Peer corroboration:" in revised.summary:
        return revised

    throughput_drop = any(
        isinstance(event, dict) and _throughput_below(event)
        for event in revised.key_events
    )
    routing_reroute = any(
        peer.agent_id == "routing"
        and peer.anomaly_detected
        and (
            "reroute" in peer.summary.lower()
            or any(
                isinstance(event, dict) and str(event.get("change_type", "")).lower() == "reroute"
                for event in peer.key_events
            )
        )
        for peer in peer_findings
    )

    if throughput_drop and routing_reroute:
        revised.revised = True
        revised.revision_count += 1
        revised.confidence = max(revised.confidence, 0.92)
        revised.summary = (
            f"{revised.summary}. "
            "Peer corroboration: routing agent reported reroute during throughput drop; "
            "metrics confidence elevated to 0.92"
        )

    return revised


def build_metrics_app(cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="netcortex-metrics-agent")
    provider = create_metrics_provider(cfg or {})
    baseline_cfg = (cfg or {}).get("baselines", {})
    baseline_provider_name = str(baseline_cfg.get("provider", "simulation")).lower()
    if baseline_provider_name == "simulation":
        baseline_provider = SimulationBaselineProvider()
    elif baseline_provider_name == "prometheus":
        baseline_provider = PrometheusBaselineProvider()
    else:
        raise ValueError("ConfigValidationError: baselines.provider must be either 'simulation' or 'prometheus'")
    z_threshold = float(baseline_cfg.get("metrics_z_threshold", 3.0))
    legacy_fallback = bool(baseline_cfg.get("legacy_fallback", True))

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return {
            "name": "netcortex-metrics-agent",
            "version": "1.0.0",
            "description": "Analyzes metric time-series for anomaly windows and degradation signals.",
            "url": "http://localhost:8001/a2a",
            "endpoint": "http://localhost:8001/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "analyze-metrics",
                    "name": "Analyze Metrics",
                    "description": "Analyze metrics in a time window and produce an AgentFinding.",
                    "tags": ["metrics", "anomaly", "rca"],
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

        if skill == "analyze-metrics":
            metrics = provider.get_metrics(data["region"], int(data["window_minutes"]), data.get("scenario_id"))
            data_degraded = getattr(provider, "degraded", False)
            impacted: list[tuple[dict, list[str]]] = []
            for metric in metrics:
                metric_row = metric.model_dump(mode="json")
                entity_keys = _metric_entity_keys(metric_row)
                reasons: list[str] = []
                had_baseline = False
                for metric_name, value in (
                    ("error_rate", metric.error_rate),
                    ("packet_loss", metric.packet_loss),
                    ("throughput_gbps", metric.throughput_gbps),
                ):
                    selected = None
                    for key in entity_keys:
                        candidate = baseline_provider.get_baseline(key, metric_name)
                        if candidate is not None:
                            selected = candidate
                            break
                    if selected is None:
                        continue
                    had_baseline = True
                    z_score = compute_z_score(float(value), selected)
                    if is_anomalous(float(value), selected, z_threshold=z_threshold):
                        reasons.append(f"{metric_name} z={z_score:.2f}")

                # Fallback keeps behavior safe when no baseline exists for an entity.
                if legacy_fallback and not had_baseline and (metric.error_rate > 2 or metric.packet_loss > 2 or metric.throughput_gbps < 0.7):
                    reasons.append("legacy-threshold")

                if reasons:
                    impacted.append((metric_row, reasons))
            anomaly = len(impacted) > 0
            summary = "Metrics within baseline"
            if anomaly:
                worst, worst_reasons = max(impacted, key=lambda x: (float(x[0].get("error_rate", 0)) + float(x[0].get("packet_loss", 0))))
                tags = worst.get("tags", {}) if isinstance(worst.get("tags", {}), dict) else {}
                node = tags.get("switch", "unknown")
                summary = (
                    f"Metric anomalies detected on {len(impacted)}/{len(metrics)} nodes; "
                    f"worst switch={node}, error_rate={float(worst.get('error_rate', 0.0)):.2f}%, "
                    f"packet_loss={float(worst.get('packet_loss', 0.0)):.2f}%, throughput={float(worst.get('throughput_gbps', 0.0)):.2f}Gbps; "
                    f"evidence={', '.join(worst_reasons)}"
                )
            logger.info(
                "Analyzed metrics region=%s window=%s scenario=%s points=%s anomaly=%s data_degraded=%s",
                data["region"],
                data["window_minutes"],
                data.get("scenario_id"),
                len(metrics),
                anomaly,
                data_degraded,
            )
            finding = AgentFinding(
                agent_id="metrics",
                domain="metrics",
                anomaly_detected=anomaly,
                summary=summary,
                key_events=[row for row, _ in impacted[:3]] if anomaly else [m.model_dump(mode="json") for m in metrics[:3]],
                start_time=min((m.timestamp for m in metrics), default=datetime.now(timezone.utc)),
                end_time=max((m.timestamp for m in metrics), default=datetime.now(timezone.utc)),
                confidence=min(0.95, 0.6 + 0.08 * len(impacted)) if anomaly else 0.25,
                data_degraded=data_degraded,
            )
            logger.info("Completed analyze-metrics anomaly=%s confidence=%.2f", finding.anomaly_detected, finding.confidence)
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
