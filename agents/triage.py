"""Per-agent LLM triage: read what no rule judged, propose hypotheses.

Called from a domain agent's plan() in each collaboration round, only when
the agent holds unclassified rows the LLM has not read yet: its own in the
first round, then what its scoped answers to peers turned up (which can be
older than the incident window). The LLM sees that evidence (with
the line text), the incident, the alarm and its peers' summaries, and may
propose up to MAX_PER_AGENT hypotheses. Each one becomes a validation_request
to this same agent, which checks it with code (detection/hypotheses.py)
before anything it says can count as evidence.

The LLM is on when `llm.hypotheses` is true (the default) and Gemini
credentials are configured; with it off, nothing here runs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
from datetime import datetime
from typing import Any

from detection.hypotheses import MAX_PER_AGENT, parse_rule, rule_hash
from detection.investigate import TopologyGraph
from models.schemas import AgentFinding

logger = logging.getLogger("net_cortex.triage")

MAX_ROWS_IN_PROMPT = 25


def llm_enabled(cfg: dict[str, Any]) -> bool:
    llm = cfg.get("llm", {}) or {}
    if not llm.get("hypotheses", True):
        return False
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT")):
        return False
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


def _ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _row_text(row: dict[str, Any]) -> str:
    value = ""
    if row.get("value") is not None:
        value = f" value={row.get('value')}"
        if "min" in row:
            value += f" (min {row['min']}, max {row['max']})"
    text = f' text="{row["text"]}"' if row.get("text") else ""
    refs = ", ".join(row.get("evidence_refs") or [row.get("evidence_ref")])
    return (f"- signal={row.get('name')} type={row.get('signal_type')} entity={row.get('entity')} severity={row.get('severity')}"
            f" first={row.get('timestamp')} last={row.get('last_seen', row.get('timestamp'))} count={row.get('count', 1)}"
            f"{value}{text} refs=[{refs}]")


def build_prompt(domain_id: str, own: AgentFinding, peers: list[AgentFinding], context: dict[str, Any]) -> str:
    rows = own.unclassified_evidence[:MAX_ROWS_IN_PROMPT]
    peer_lines = "\n".join(f"- {p.domain}: anomaly={p.anomaly_detected} {p.summary[:300]}" for p in peers) or "none"
    alarm = ", ".join(f"{e.get('type')}:{e.get('id')}" for e in context.get("alarm_entities") or []) or "none"
    return (
        f"You are the {domain_id} agent in a network root cause analysis. Your detection rules did not judge the "
        "evidence below: no catalog rule knows these signals or log lines. Decide whether any of it explains the "
        "incident.\n\n"
        f"Incident: {context.get('incident_description', '')}\n"
        f"Alarm entities: {alarm}\n"
        f"Window: {context.get('start_time')} .. {context.get('end_time')}\n"
        f"Your rule-based finding: {own.summary[:400]}\n"
        f"Peer findings:\n{peer_lines}\n\n"
        f"Evidence no rule judged:\n" + "\n".join(_row_text(r) for r in rows) + "\n\n"
        "Propose a hypothesis only for evidence that could cause the incident (a change, an error, a failure), "
        f"at most {MAX_PER_AGENT}. Routine noise, read-only calls and healthy status lines are not hypotheses. "
        "Each hypothesis is a temporary detection rule that code will check against fresh data: the rule must "
        "fire on the cited evidence now, must not fire in an earlier window, and the entity must sit on or "
        "upstream of the alarm. An empty list is a good answer when nothing explains the incident.\n\n"
        "Rule types (the only ones allowed):\n"
        '  {"type": "event_presence", "min_severity": "info"}   the signal occurs at all\n'
        '  {"type": "threshold", "op": "gt"|"lt", "value": <number>}   on the value\n'
        '  {"type": "threshold", "op": "between", "min": <n>, "max": <n>}\n'
        '  {"type": "state_equals", "value": <number|string>}   value equals a state\n'
        '  {"type": "rate_change", "direction": "increase"|"decrease", "factor": <number > 1>}\n\n'
        "Return ONLY JSON: {\"hypotheses\": [{\n"
        '  "evidence_refs": [refs copied exactly from ONE evidence line above],\n'
        '  "signal": "<that line\'s signal name>",\n'
        '  "rule": {...},\n'
        '  "role": "cause" or "symptom",\n'
        '  "why": "<one sentence: what went wrong and why it explains the alarm>",\n'
        '  "proposed_name": "<snake_case name for a new catalog signal>",\n'
        '  "description": "<one line for the catalog>",\n'
        '  "classify_pattern": "<for a log line: a regex that matches its text, else null>"\n'
        "}]}\n"
    )


def _call(prompt: str, model: str) -> dict | None:
    from agents.rca_synthesizer import _call_gemini

    return _call_gemini(prompt, model)


def to_requests(raw: Any, domain_id: str, own: AgentFinding, context: dict[str, Any], topology: Any,
                catalog: Any) -> list[dict[str, Any]]:
    """Validate the LLM's hypotheses (closed rule set, cited refs from one row)
    and turn each into a validation_request to this agent. Invalid ones are
    dropped here; everything else is decided by the code checks."""
    items = raw.get("hypotheses") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    by_ref: dict[str, dict[str, Any]] = {}
    for row in own.unclassified_evidence:
        for ref in row.get("evidence_refs") or [row.get("evidence_ref")]:
            if ref:
                by_ref[ref] = row
    graph = TopologyGraph.build(topology, catalog)
    out: list[dict[str, Any]] = []
    for item in items[:MAX_PER_AGENT]:
        if not isinstance(item, dict):
            continue
        refs = [str(r) for r in item.get("evidence_refs") or [] if r]
        rows = [by_ref[r] for r in refs if r in by_ref]
        if not refs or not rows:
            logger.info("Triage %s: dropped a hypothesis citing unknown refs %s", domain_id, refs[:3])
            continue
        entities = {r["entity"] for r in rows}
        names = {r["name"] for r in rows}
        if len(entities) != 1 or len(names) != 1:
            logger.info("Triage %s: dropped a hypothesis spanning %s entities / %s signals", domain_id,
                        len(entities), len(names))
            continue
        etype, _, eid = next(iter(entities)).partition(":")
        entity = {"type": etype, "id": eid}
        signal = next(iter(names))
        try:
            rule = parse_rule(item.get("rule"))
        except ValueError as exc:
            logger.info("Triage %s: dropped a hypothesis on %s: %s", domain_id, signal, exc)
            continue
        rid = rule_hash(signal, entity, rule)
        node = graph.resolve(etype, eid) if graph else None
        aliases = graph.aliases(node) if node is not None else [eid]
        request_id = hashlib.sha1(f"hypothesis|{domain_id}|{rid}".encode()).hexdigest()[:12]
        # The window reaches back to the cited line when a scoped answer found it
        # before this incident's window; the "new" check then looks before that.
        start = min([context["start_time"], *(str(r["timestamp"]) for r in rows)], key=_ts)
        hypothesis = {
            "id": rid.split(":", 1)[1], "rule_id": rid, "domain": domain_id, "signal": signal, "entity": entity,
            "signal_type": rows[0].get("signal_type"),
            "rule": rule.model_dump(mode="json", exclude_none=True), "evidence_refs": refs,
            "role": item.get("role") if item.get("role") in ("cause", "symptom") else None,
            "why": str(item.get("why") or "")[:300], "proposed_name": str(item.get("proposed_name") or "")[:60],
            "description": str(item.get("description") or "")[:300],
            "classify_pattern": item.get("classify_pattern") if isinstance(item.get("classify_pattern"), str) else None,
            "example_text": [r["text"] for r in rows if r.get("text")][:3],
        }
        out.append({
            "request_id": request_id, "target": domain_id, "sender": domain_id, "entity": entity,
            "entity_filters": [{"type": etype, "id": a} for a in aliases],
            "start_time": _ts(start).isoformat(), "end_time": context["end_time"],
            "anchor": {"type": etype, "id": eid, "origin": "hypothesis"}, "path": [f"{etype}:{eid}"], "hops": 0,
            "question": f"{domain_id} hypothesis {rid}: {signal} on {etype}:{eid} fires "
                        f"{json.dumps(hypothesis['rule'], sort_keys=True)} ({hypothesis['why']})",
            "hypothesis": hypothesis,
        })
    return out
