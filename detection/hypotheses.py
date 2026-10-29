"""Hypotheses: temporary rules an LLM proposes, checked by code.

An agent's LLM triage reads the evidence no catalog rule judged (its
`unclassified_evidence`) and may propose a hypothesis: "this raw signal on
this entity is what went wrong, and this rule says so". The rule must come
from the closed rule set (detection/rules.py). The hypothesis then travels
as a validation_request to the agent that owns the telemetry, which accepts
it only if four code checks pass on fresh, scoped reads:

  grounded  every evidence_ref it cites is in what the agent fetched
  fires     the temporary rule fires on the scoped query
  new       it does not fire in an equal window before the lookback
  placed    the entity is on the alarm, or upstream of it in the topology

A supported hypothesis becomes an evidence row with via="hypothesis", rule
id `llm:<hash>` and role None, so it ranks above a symptom and below a
catalog cause. Supported rules are also offered as draft catalog entries
(proposed_signals.json) for a human to approve.

Pure functions; the agent does the fetching.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import TypeAdapter, ValidationError

from contract import Signal
from contract.catalog_loader import Catalog
from contract.catalog_models import Rule, SignalDef
from detection.investigate import TopologyGraph, _targets
from detection.rules import RuleContext, evaluate, series_key
from models.schemas import AgentFinding

MAX_PER_AGENT = 3
CHECKS = ("grounded", "fires", "new", "placed")
_RULE = TypeAdapter(Rule)


def parse_rule(raw: Any) -> Any:
    """A temporary rule, validated against the closed rule set. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("rule must be an object")
    raw = {k: v for k, v in raw.items() if k != "id"}
    try:
        rule = _RULE.validate_python(raw)
    except ValidationError as exc:
        raise ValueError(f"rule is not in the closed rule set: {exc.errors()[0]['msg']}") from None
    if rule.type == "threshold" and rule.op != "between" and rule.value is None:
        raise ValueError("threshold rule needs a value")
    if rule.type == "threshold" and rule.op == "between" and (rule.min is None or rule.max is None):
        raise ValueError("between needs min and max")
    if rule.type == "zscore_baseline":
        raise ValueError("zscore_baseline needs a baseline store; propose threshold, state_equals, rate_change "
                         "or event_presence")
    return rule


def rule_hash(signal: str, entity: dict[str, str], rule: Any) -> str:
    raw = json.dumps({"signal": signal, "entity": entity, "rule": rule.model_dump(mode="json", exclude_none=True)},
                     sort_keys=True)
    return "llm:" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def _matches(signal: Signal, name: str, aliases: set[tuple[str, str]]) -> bool:
    return signal.name == name and (signal.entity.type, signal.entity.id) in aliases


def fire(rule: Any, rule_id: str, name: str, signals: list[Signal], aliases: set[tuple[str, str]],
         region: str) -> list[tuple[Signal, Any]]:
    """(signal, hit) for every sample of `name` on the entity the rule fires on."""
    mine = sorted((s for s in signals if _matches(s, name, aliases)), key=lambda s: s.timestamp)
    ctx = RuleContext(region=region, legacy_fallback=False)
    for s in mine:
        ctx.series.setdefault(series_key(s), []).append(s)
    out = []
    for s in mine:
        hit = evaluate(rule, s, ctx, rule_id=rule_id, catalog_names=[name])
        if hit is not None:
            out.append((s, hit))
    return out


def placed(entity: dict[str, str], topology: Any, catalog: Catalog, alarm_entities: list[dict],
           findings: list[AgentFinding]) -> bool:
    """On the alarm (or, without alarm entities, on a symptom) or upstream of it."""
    graph = TopologyGraph.build(topology, catalog)
    if not graph:
        return False
    node = graph.resolve(entity["type"], entity["id"])
    if node is None:
        return False
    targets = _targets(findings, graph, alarm_entities, catalog)
    max_hops = catalog.investigation.walk.max_hops
    return any(node == t or graph.depends_on(t, node, max_hops) for t in targets)


def signal_text(signal: Signal) -> str:
    for attr in ("message", "sample", "details", "request"):
        if attr in signal.attributes:
            return str(signal.attributes[attr])
    return ""


def evidence_row(hypothesis: dict[str, Any], hits: list[tuple[Signal, Any]]) -> dict[str, Any]:
    signals = [s for s, _ in hits]
    first = min(s.timestamp for s in signals).isoformat()
    reason = hits[0][1].reason
    return {
        "entity_type": hypothesis["entity"]["type"], "entity_id": hypothesis["entity"]["id"],
        "signals": [hypothesis["signal"]], "role": None,
        "severity": max((s.severity for s in signals), key=lambda sev: ["debug", "info", "warning", "error",
                                                                         "critical"].index(sev)),
        "first_seen": first, "last_seen": max(s.timestamp for s in signals).isoformat(), "onset": first,
        "count": len(signals),
        "rules_fired": [{"signal": hypothesis["signal"], "rule": hypothesis["rule_id"], "reason": reason,
                         "evidence_ref": signals[0].evidence_ref}],
        "evidence_refs": list(dict.fromkeys(s.evidence_ref for s in signals if s.evidence_ref))[:5],
        "via": "hypothesis", "hypothesis_why": hypothesis.get("why", ""),
    }


def key_event(hypothesis: dict[str, Any], hits: list[tuple[Signal, Any]]) -> dict[str, Any]:
    s, hit = hits[0]
    return {
        "timestamp": s.timestamp.isoformat(), "entity_type": s.entity.type, "entity_id": s.entity.id,
        "name": s.name, "severity": s.severity, "value": s.value, "text": signal_text(s)[:300],
        "rules_fired": [{"signal": hypothesis["signal"], "rule": hypothesis["rule_id"], "reason": hit.reason,
                         "evidence_ref": s.evidence_ref}],
        "evidence_refs": [x.evidence_ref for x, _ in hits if x.evidence_ref][:5],
        "via": "hypothesis", **({"count": len(hits)} if len(hits) > 1 else {}),
    }


def verify_pattern(pattern: Any, texts: list[str]) -> str | None:
    """A source-side classify pattern the LLM suggested, kept only if it compiles
    and matches every cited line."""
    if not isinstance(pattern, str) or not pattern or not texts:
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    return pattern if all(compiled.search(t) for t in texts) else None


def draft_entries(hypotheses: list[dict[str, Any]], catalog: Catalog) -> dict[str, Any]:
    """Draft signals.json entries for the supported hypotheses (for human review)."""
    signals: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    for h in hypotheses:
        if h.get("verdict") != "supported":
            continue
        name = re.sub(r"[^a-z0-9_]", "_", str(h.get("proposed_name") or h["signal"]).lower()).strip("_") or "llm_signal"
        if catalog.signal(name) is not None and name != h["signal"]:
            name = f"{name}_proposed"
        rule = parse_rule(h["rule"]).model_dump(mode="json", exclude_none=True)
        rule.pop("id", None)
        entry = {
            "signal_type": h.get("signal_type") or "event", "domain": h["domain"],
            "description": str(h.get("description") or h.get("why") or name)[:300],
            "entity_types": [h["entity"]["type"]] if h["entity"]["type"] in catalog.entities.entity_types else [],
            "rule": rule,
        }
        if name != h["signal"]:
            entry["match_names"] = [h["signal"]]
        SignalDef.model_validate(entry)
        signals[name] = entry
        notes[name] = {
            "status": "draft: verified once by code, needs human approval",
            "hypothesis_id": h["id"], "rule_id": h["rule_id"], "role_suggested": h.get("role"),
            "why": h.get("why", ""), "entity": h["entity"], "evidence_refs": h.get("evidence_refs", []),
            "checks": h.get("checks", {}), "example_text": h.get("example_text", [])[:3],
            "classify_pattern": h.get("classify_pattern"),
        }
    return {"catalog_version": catalog.signals.catalog_version, "signals": signals, "review": notes}


def window_before(start: datetime, end: datetime, lookback_minutes: int) -> tuple[datetime, datetime]:
    """The equal-length window that ends `lookback_minutes` before `start`."""
    span = end - start
    earlier_end = start - timedelta(minutes=lookback_minutes)
    return earlier_end - span, earlier_end
