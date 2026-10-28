from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from contract.catalog_loader import Catalog, get_catalog
from models.schemas import A2AMessage, AgentFinding, RankedCause, RCAReport


logger = logging.getLogger("net_cortex.synthesizer")


MAX_UNCLASSIFIED_IN_REPORT = 50


def _signal_glossary(findings: list[AgentFinding], catalog: Catalog) -> str:
    """One line per catalog signal that fired, from signals.json `description`."""
    lines: dict[str, str] = {}
    for f in findings:
        for event in f.key_events:
            for fired in (event.get("rules_fired") or []) if isinstance(event, dict) else []:
                found = catalog.signal(str(fired.get("signal", "")))
                if found and found[0] not in lines:
                    lines[found[0]] = f"  {found[0]}: {found[1].description}"
    return "\n".join(lines.values()) or "none"


def _unclassified_text(findings: list[AgentFinding]) -> str:
    rows = [
        f"  [{f.domain}] {u.get('name')} {u.get('entity')} value={u.get('value')} severity={u.get('severity')} "
        f"source={u.get('source')} ref={u.get('evidence_ref')}"
        for f in findings for u in f.unclassified_evidence
    ]
    return "\n".join(rows[:30]) or "none"


def _evidence_index(findings: list[AgentFinding]) -> dict[str, str]:
    """Citable evidence: E<n> -> one rule-fired evidence row. The LLM may only
    cite these ids, so every claim it makes traces to a rule that fired."""
    index: dict[str, str] = {}
    for f in findings:
        for row in f.evidence:
            rules = "; ".join(str(r.get("reason", "")) for r in row.get("rules_fired", [])[:2])
            index[f"E{len(index) + 1}"] = (
                f"[{f.domain}] {', '.join(row.get('signals', []))} on {row.get('entity_type')}:{row.get('entity_id')} "
                f"first {row.get('first_seen')} x{row.get('count', 1)} ({rules})"
                + (" [found by peer validation]" if row.get("via") == "validation" else "")
            )
    return index


def _ranked_text(ranked: list[RankedCause]) -> str:
    rows = []
    for c in ranked[:8]:
        rows.append(
            f"  {c.id}: {', '.join(c.signals)} on {c.entity_type}:{c.entity_id} ({c.domain}) first {c.first_seen.isoformat()} "
            f"score={c.score} features={c.features}" + (f" chain={' -> '.join(c.chain)}" if c.chain else "")
        )
    return "\n".join(rows) or "none"


def _build_llm_prompt(incident_description: str, findings: list[AgentFinding], messages: list[A2AMessage],
                      catalog: Catalog | None = None, ranked: list[RankedCause] | None = None,
                      evidence: dict[str, str] | None = None, status: str = "unknown") -> str:
    catalog = catalog or get_catalog()
    ranked = ranked or []
    evidence = evidence if evidence is not None else _evidence_index(findings)
    finding_blocks = []
    for f in findings:
        events_text = json.dumps(f.key_events, indent=2, default=str) if f.key_events else "none"
        finding_blocks.append(
            f"Domain: {f.domain}\n"
            f"Anomaly detected: {f.anomaly_detected}\n"
            f"Summary: {f.summary}\n"
            f"Confidence: {f.confidence}\n"
            f"Key evidence:\n{events_text}"
        )
    a2a_highlights = []
    for m in messages:
        if m.message_type == "validation_request":
            entity = m.payload.get("entity") or {}
            verdict = (m.payload.get("response") or {}).get("verdict", "?")
            a2a_highlights.append(
                f"  [{m.sender_agent} asked {m.target_agent}] {entity.get('type')}:{entity.get('id')} -> {verdict}"
            )
    a2a_text = "\n".join(a2a_highlights) if a2a_highlights else "none"
    findings_text = "\n\n".join(finding_blocks)
    return (
        "You are an expert network and systems engineer performing root cause analysis.\n"
        "Given the following multi-domain agent findings, produce a structured JSON analysis.\n\n"
        f"Incident description: {incident_description}\n\n"
        f"Domain agent findings:\n{findings_text}\n\n"
        f"Questions the agents asked each other (topology-scoped) and the answers:\n{a2a_text}\n\n"
        f"Citable evidence (every item is a rule that fired):\n"
        + ("\n".join(f"  {k}: {v}" for k, v in evidence.items()) or "  none") + "\n\n"
        f"Candidate causes ranked by code (evidence, topology position relative to the alarm, time precedence, "
        f"signal role):\n{_ranked_text(ranked)}\n\n"
        f"Incident status from the latest samples: {status}\n\n"
        f"Signal glossary (what each fired signal means):\n{_signal_glossary(findings, catalog)}\n\n"
        "Unclassified evidence (signals outside the detection catalog; no rule judged them, "
        f"weigh them cautiously):\n{_unclassified_text(findings)}\n\n"
        "Rules: base every claim on the citable evidence and cite it inline as [E<n>]. Do not cite anything else. "
        "A cause upstream of the alarm in the topology that fired first usually explains the downstream symptoms.\n\n"
        "Return ONLY valid JSON with exactly these keys:\n"
        + ("  ranked_cause_ids: the candidate cause ids (e.g. [\"C2\", \"C1\"]) in your order, most likely first\n" if ranked else "")
        + "  root_cause: one concise sentence naming the specific root cause (include component name, what changed, and magnitude if known), with [E<n>] citations\n"
        "  contributing_factors: list of strings, each citing a specific domain finding with evidence detail\n"
        "  causal_chain: ordered list of strings showing the sequence of events from trigger to user impact\n"
        "  metrics_affected: list of metric names that were measurably impacted\n"
        "  human_readable_summary: 3-5 sentences in plain English. Explain: what happened, when, what component was involved, how it cascaded, and what the observed impact was.\n"
    )


_RETRYABLE_STATUS_CODES = {429, 503}
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2.0


def _call_gemini(prompt: str, model_name: str) -> dict | None:
    try:
        from google import genai  # type: ignore
        from google.genai import errors  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError:
        logger.info("LLM disabled: google-genai package not installed")
        return None
    api_key = os.environ.get("GEMINI_API_KEY", "")
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    gcp_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if api_key:
        # Explicit API key — uses Gemini Developer API directly.
        logger.info("LLM auth mode=api_key model=%s", model_name)
        client = genai.Client(api_key=api_key)
    elif gcp_project:
        # ADC via Vertex AI — requires GOOGLE_CLOUD_PROJECT env var.
        logger.info("LLM auth mode=adc project=%s location=%s model=%s", gcp_project, gcp_location, model_name)
        client = genai.Client(vertexai=True, project=gcp_project, location=gcp_location)
    else:
        logger.info("LLM disabled: no GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT configured")
        return None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            return json.loads(response.text)
        except errors.APIError as exc:
            if exc.code in _RETRYABLE_STATUS_CODES and attempt < _MAX_ATTEMPTS:
                logger.warning(
                    "LLM call attempt %d/%d failed with transient error, retrying: %s",
                    attempt, _MAX_ATTEMPTS, exc,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue
            logger.warning("LLM call failed, using deterministic fallback: %s", exc)
            return None
        except Exception as exc:
            logger.warning("LLM call failed, using deterministic fallback: %s", exc)
            return None
    return None


def compute_confidence(findings: list[AgentFinding], total_agents: int) -> tuple[float, int, bool]:
    # total is the number of agents *dispatched*, not the number that
    # survived (len(findings)). Basing it on survivors meant that losing
    # agents to timeout/error shrank the denominator and *increased*
    # reported confidence — e.g. 4 findings with 1 anomalous gives
    # 0.25 * 0.50 * 0.9 = 0.11, but if the 3 non-anomalous agents time out
    # (leaving 1 finding) the old formula gave 1.0 * 0.50 * 1.0 = 0.50.
    # max(..., len(findings), 1) is defensive only: total_agents should
    # always be >= len(findings) since findings only contains agents that
    # actually responded.
    total = max(total_agents, len(findings), 1)
    corroborating = sum(1 for f in findings if f.anomaly_detected)
    agreeing = corroborating

    if agreeing <= 1:
        weight = 0.50
    elif agreeing == 2:
        weight = 0.75
    elif agreeing == 3:
        weight = 0.90
    else:
        weight = 1.00

    conflict = corroborating > 0 and corroborating < len(findings)
    penalty = 0.10 if conflict else 0.0
    confidence = (corroborating / total) * weight * (1 - penalty)
    return round(confidence, 2), corroborating, conflict


def _coerce_llm_field(llm_result: dict, key: str, expected_type: type, fallback):
    """Validate one field's shape before trusting LLM output structurally.

    The LLM is asked for JSON but nothing upstream validates it — a
    parseable-but-wrong-typed field (e.g. a string where a list was
    expected) previously raised an uncaught pydantic ValidationError deep
    in RCAReport construction and crashed synthesizer_node. Falls back to
    the deterministic value for just that field instead.
    """
    if key not in llm_result:
        return fallback
    value = llm_result[key]
    if not isinstance(value, expected_type):
        logger.warning(
            "LLM field '%s' has unexpected type %s (expected %s); using deterministic fallback",
            key, type(value).__name__, expected_type.__name__,
        )
        return fallback
    if expected_type is list and not all(isinstance(item, str) for item in value):
        logger.warning("LLM field '%s' list contains non-string items; using deterministic fallback", key)
        return fallback
    return value


_CITATION = re.compile(r"\[(E\d+)\]")


def _cited_ok(text: str, evidence: dict[str, str]) -> bool:
    """A claim must cite only known evidence ids (uncited prose is allowed only
    when there is no evidence to cite)."""
    cited = _CITATION.findall(text)
    if not evidence:
        return True
    return bool(cited) and all(c in evidence for c in cited)


def _rerank(ranked: list[RankedCause], llm_result: dict | None) -> list[RankedCause]:
    """The LLM's order, if it is a permutation of known ids (extras ignored,
    omissions appended in code order); otherwise the code's order."""
    ids = llm_result.get("ranked_cause_ids") if llm_result else None
    if not ranked or not isinstance(ids, list):
        return ranked
    by_id = {c.id: c for c in ranked}
    chosen = [by_id[i] for i in dict.fromkeys(str(x) for x in ids) if i in by_id]
    if not chosen:
        logger.warning("LLM ranked_cause_ids named no known cause; keeping the deterministic order")
        return ranked
    return chosen + [c for c in ranked if c not in chosen]


def _status_note(status: str) -> str:
    if status == "recovered":
        return " The affected entities have since recovered (no longer firing)."
    if status == "flapping":
        return " The affected entities are flapping (repeated down/up), not a steady outage."
    return ""


def _deterministic_from_cause(top: RankedCause, findings: list[AgentFinding], alarm_entities: list[dict]) -> str:
    alarm = ", ".join(f"{e.get('type')}:{e.get('id')}" for e in alarm_entities) if alarm_entities else ""
    where = f" via {' -> '.join(top.chain)}" if top.chain else ""
    upstream = f", upstream of the alarm on {alarm}{where}" if alarm and top.features.get("topology", 0) >= 1.0 else (
        f", linked to the alarm{where}" if where else "")
    found = " (found by peer validation)" if top.via_validation else ""
    summary = next((f.summary for f in findings if f.domain == top.domain), "")
    return (f"{top.domain.capitalize()} domain: {', '.join(top.signals)} on {top.entity_type} {top.entity_id} "
            f"at {top.first_seen.strftime('%H:%Mz')}{found}{upstream} - {summary}")


def synthesize_report(
    incident_id: str,
    findings: list[AgentFinding],
    messages: list[A2AMessage],
    incident_description: str = "",
    llm_model: str = "gemini-2.5-flash",
    require_llm: bool = False,
    total_agents: int | None = None,
    timed_out_agents: list[str] | None = None,
    errored_agents: list[str] | None = None,
    catalog: Catalog | None = None,
    ranked_causes: list[RankedCause] | None = None,
    alarm_entities: list[dict] | None = None,
    status: str = "unknown",
) -> RCAReport:
    catalog = catalog or get_catalog()
    timed_out_agents = list(timed_out_agents or [])
    errored_agents = list(errored_agents or [])
    resolved_total_agents = total_agents if total_agents is not None else len(findings)
    score, corroborating_count, conflict = compute_confidence(findings, resolved_total_agents)
    anomaly_findings = [f for f in findings if f.anomaly_detected]
    data_degraded = any(f.data_degraded for f in findings)
    degraded_domains = sorted({f.domain for f in findings if f.data_degraded})

    # --- Deterministic fallback (always computed, used if LLM unavailable) ---
    if not anomaly_findings:
        if data_degraded:
            # A total telemetry outage degrades every adapter to an empty
            # result at WARNING with no signal the caller can see — which
            # made should_short_circuit's "all no-anomaly" case read as a
            # confident, clean report indistinguishable from a verified
            # absence of anomalies. Surface the distinction instead.
            det_root = (
                "RCA inconclusive: telemetry could not be retrieved for "
                f"{', '.join(degraded_domains)}, so 'no anomaly' cannot be verified."
            )
            det_contributing = [
                f"{domain}: telemetry fetch failed or was degraded during this window"
                for domain in degraded_domains
            ]
            det_chain = ["Telemetry collection failed before analysis could complete"]
            det_affected: list[str] = []
            det_summary = (
                "One or more domains could not retrieve telemetry for this incident window, so this "
                "report reflects missing data rather than a verified absence of anomalies. "
                f"Affected domains: {', '.join(degraded_domains)}."
            )
        else:
            det_root = "No anomaly detected across all monitored domains."
            det_contributing = ["No corroborating anomalies across domains"]
            det_chain = ["Signals remained within baseline noise"]
            det_affected: list[str] = []
            det_summary = "All domain agents reported metrics within normal baseline. No root cause was identified."
    else:
        top = sorted(anomaly_findings, key=lambda f: f.confidence, reverse=True)[0]
        ranked_causes = list(ranked_causes or [])
        # Build a richer deterministic root cause from the top finding's key_events
        first_event = top.key_events[0] if top.key_events else {}
        component = first_event.get("component", first_event.get("path_id", ""))
        change_type = first_event.get("change_type", "")
        before = first_event.get("before", {})
        after = first_event.get("after", {})
        if component and change_type:
            change_detail = f"{change_type} on {component}"
            if before and after:
                change_detail += f" (changed from {before} to {after})"
            det_root = f"{top.domain.capitalize()} domain: {change_detail} - {top.summary}"
        else:
            det_root = f"{top.domain.capitalize()} domain anomaly: {top.summary}"
        if ranked_causes:
            det_root = _deterministic_from_cause(ranked_causes[0], findings, alarm_entities or [])
        det_root += _status_note(status)

        det_contributing = [
            f"{f.domain} ({f.confidence:.0%} confidence): {f.summary}"
            + (f" - key event: {f.key_events[0]}" if f.key_events else "")
            for f in anomaly_findings
        ]
        det_chain = []
        for f in sorted(anomaly_findings, key=lambda x: x.start_time):
            det_chain.append(f"{f.start_time.strftime('%H:%Mz')}: {f.domain} detected -- {f.summary}")
        det_affected = list({
            m for f in anomaly_findings
            for m in (catalog.domain(f.domain).metrics_affected if f.domain in catalog.domains.domains else [f"{f.domain}_state"])
        })
        corroborating_names = ", ".join(f.domain for f in anomaly_findings)
        det_summary = (
            f"The investigation found anomalies in {len(anomaly_findings)} domain(s): {corroborating_names}. "
            f"The highest-confidence signal came from {top.domain} ({top.confidence:.0%}): {top.summary}. "
            f"Confidence score: {score:.0%}."
            + (" Conflicting signals were detected across domains." if conflict else "")
            + (
                f" Note: telemetry from [{', '.join(degraded_domains)}] could not be fully retrieved "
                "during this window."
                if degraded_domains else ""
            )
        )

    # --- LLM enrichment (optional, overwrites deterministic values if successful) ---
    llm_result: dict | None = None
    ranked_causes = list(ranked_causes or [])
    evidence = _evidence_index(findings)
    if incident_description and anomaly_findings:
        prompt = _build_llm_prompt(incident_description, findings, messages, catalog, ranked_causes, evidence, status)
        llm_result = _call_gemini(prompt, llm_model)
    if llm_result and ranked_causes:
        ranked_causes = _rerank(ranked_causes, llm_result)
    if llm_result and isinstance(llm_result.get("root_cause"), str) and not _cited_ok(llm_result["root_cause"], evidence):
        logger.warning("LLM root_cause cites no known rule-fired evidence; using the deterministic root cause")
        llm_result = {k: v for k, v in llm_result.items() if k != "root_cause"}

    if require_llm and anomaly_findings and llm_result is None:
        raise RuntimeError(
            "LLM synthesis is required but unavailable. "
            "Check Gemini auth/project permissions and verify aiplatform.endpoints.predict access."
        )

    # Each field is validated and falls back independently — an LLM response
    # that got one field's shape wrong (e.g. a string where a list was
    # expected) no longer takes down the whole report; see _coerce_llm_field.
    root = _coerce_llm_field(llm_result, "root_cause", str, det_root) if llm_result else det_root
    contributing = _coerce_llm_field(llm_result, "contributing_factors", list, det_contributing) if llm_result else det_contributing
    chain = _coerce_llm_field(llm_result, "causal_chain", list, det_chain) if llm_result else det_chain
    affected = _coerce_llm_field(llm_result, "metrics_affected", list, det_affected) if llm_result else det_affected
    summary = _coerce_llm_field(llm_result, "human_readable_summary", str, det_summary) if llm_result else det_summary

    return RCAReport(
        incident_id=incident_id,
        root_cause=root,
        contributing_factors=contributing,
        causal_chain=chain,
        metrics_affected=affected,
        human_readable_summary=summary,
        agent_findings=findings,
        a2a_message_log=messages,
        confidence_score=score,
        corroborating_domain_count=corroborating_count,
        conflict_detected=conflict,
        timed_out_agents=timed_out_agents,
        errored_agents=errored_agents,
        data_degraded=data_degraded,
        unclassified_evidence=[
            {"domain": f.domain, **u} for f in findings for u in f.unclassified_evidence
        ][:MAX_UNCLASSIFIED_IN_REPORT],
        alarm_entities=list(alarm_entities or []),
        ranked_causes=ranked_causes,
        incident_status=status if status in ("ongoing", "recovered", "flapping") else "unknown",
        generated_at=datetime.now(timezone.utc),
    )
