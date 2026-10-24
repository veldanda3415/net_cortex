"""Cross-domain reconsideration driven by correlation.json.

For a finding, rules of its domain are tried in priority order; the first
whose conditions hold is applied (confidence adjustment + a note appended
to the summary) and evaluation stops. A rule whose marker ("Peer
corroboration:" / "Peer contradiction:") is already in the summary is
skipped, which keeps repeated collaboration rounds idempotent.
"""

from __future__ import annotations

from typing import Any

from contract.catalog_loader import Catalog
from contract.catalog_models import Condition, KeyEventCondition, PeerScope
from detection.templates import render
from models.schemas import AgentFinding

MARKERS = {"corroboration": "Peer corroboration:", "contradiction": "Peer contradiction:"}


def _scope(spec: PeerScope, me: AgentFinding, peers: list[AgentFinding]) -> list[AgentFinding]:
    if spec.scope == "self":
        return [me]
    return [
        p for p in peers
        if (spec.domains is None or p.domain in spec.domains) and (not spec.peer_anomaly or p.anomaly_detected)
    ]


def _compare(actual: Any, op: str, expected: Any) -> bool:
    if actual is None:
        return False
    if op in ("lt", "gt"):
        try:
            a, e = float(actual), float(expected)
        except (TypeError, ValueError):
            return False
        return a < e if op == "lt" else a > e
    if op == "contains":
        return str(expected).lower() in str(actual).lower()
    if isinstance(expected, str) or isinstance(actual, str):
        return str(actual).lower() == str(expected).lower()
    return actual == expected


def _key_event_match(cond: KeyEventCondition, findings: list[AgentFinding]) -> bool:
    return any(
        isinstance(event, dict) and _compare(event.get(cond.field), cond.op, cond.value)
        for f in findings for event in f.key_events
    )


def holds(cond: Condition, me: AgentFinding, peers: list[AgentFinding]) -> bool:
    if cond.key_event is not None:
        return _key_event_match(cond.key_event, _scope(cond.key_event, me, peers))
    if cond.summary_contains is not None:
        text = cond.summary_contains.text.lower()
        return any(text in f.summary.lower() for f in _scope(cond.summary_contains, me, peers))
    if cond.peer_anomaly is not None:
        wanted = cond.peer_anomaly.domains
        return any(p.anomaly_detected and (wanted is None or p.domain in wanted) for p in peers)
    if cond.any is not None:
        return any(holds(c, me, peers) for c in cond.any)
    if cond.all is not None:
        return all(holds(c, me, peers) for c in cond.all)
    return False


def reconsider(finding: AgentFinding, peers: list[AgentFinding], catalog: Catalog) -> AgentFinding:
    revised = finding.model_copy(deep=True)
    peer_anomaly_domains = ", ".join(sorted({p.domain for p in peers if p.anomaly_detected}))
    for _, rule in catalog.correlation_rules(finding.domain):
        if rule.when.self_anomaly != revised.anomaly_detected:
            continue
        if MARKERS[rule.effect.kind] in revised.summary:
            continue
        if not all(holds(c, revised, peers) for c in rule.when.all):
            continue
        effect = rule.effect
        if effect.confidence.op == "at_least":
            revised.confidence = max(revised.confidence, effect.confidence.value)
        else:
            raised = revised.confidence + effect.confidence.value
            revised.confidence = min(effect.confidence.cap, raised) if effect.confidence.cap is not None else raised
        revised.revised = True
        revised.revision_count += 1
        note = render(effect.note, {"peer_anomaly_domains": peer_anomaly_domains})
        revised.summary = f"{revised.summary}. {note}"
        return revised
    return revised
