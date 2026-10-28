"""Collaboration reasoning over topology: which questions to ask peers, how
to read the answers, and how to rank the candidate causes.

Pure functions of (findings, topology, catalog). The orchestrator routes the
messages and the agents answer them by re-running their normal detection on a
scoped query, so every piece of evidence still comes from a catalog rule.

  plan_requests   an agent, having seen its peers' findings, asks the domains
                  that can see the entities around its anomalies (and the
                  alarm), over a window that reaches back before the first
                  symptom
  verdict         a scoped answer is supported / refuted / inconclusive
  find_leads      ids a scoped answer names that nobody asked about yet
  rank_causes     every entity with rule-fired evidence, scored on evidence,
                  topology position, time precedence and signal role
  incident_status ongoing / recovered / flapping from the agents' entity_status

All tuning lives in contract/catalog/investigation.json.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from contract import TopologyResponse
from contract.catalog_loader import Catalog
from models.schemas import AgentFinding, RankedCause

SEVERITY_SCORE = {"debug": 0.1, "info": 0.25, "warning": 0.5, "error": 0.75, "critical": 1.0}
ROLE_SCORE = {"cause": 1.0, None: 0.5, "symptom": 0.0}

Node = tuple[str, str]  # (entity type, entity id)


def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def node_key(node: Node) -> str:
    return f"{node[0]}:{node[1]}"


# ------------------------------------------------------------------- topology

@dataclass
class TopologyGraph:
    """Undirected adjacency for walking, plus `depends` edges (from the end
    that depends to the end it depends on) for "is X upstream of Y?".

    Entities are matched by id or by any label value (an alias): CloudWatch
    names a tunnel by its outside IP, the topology by its tunnel id."""

    nodes: dict[Node, dict[str, str]] = field(default_factory=dict)
    adjacent: dict[Node, set[Node]] = field(default_factory=dict)
    depends: dict[Node, set[Node]] = field(default_factory=dict)
    _alias: dict[tuple[str, str], Node] = field(default_factory=dict)

    @classmethod
    def build(cls, topology: TopologyResponse | dict | None, catalog: Catalog) -> "TopologyGraph":
        graph = cls()
        if topology is None:
            return graph
        if isinstance(topology, dict):
            topology = TopologyResponse.model_validate(topology)
        for entity in topology.entities:
            graph._add(entity.type, entity.id, entity.labels)
        relationships = catalog.entities.relationships
        for edge in topology.relationships:
            a = graph._add(edge.source.type, edge.source.id, edge.source.labels)
            b = graph._add(edge.target.type, edge.target.id, edge.target.labels)
            graph.adjacent.setdefault(a, set()).add(b)
            graph.adjacent.setdefault(b, set()).add(a)
            rel = relationships.get(edge.type)
            dependent = rel.dependent if rel is not None else "both"
            if dependent in ("source", "both"):
                graph.depends.setdefault(a, set()).add(b)
            if dependent in ("target", "both"):
                graph.depends.setdefault(b, set()).add(a)
        return graph

    def _add(self, etype: str, eid: str, labels: dict[str, str]) -> Node:
        node = (etype, eid)
        merged = {**labels, **self.nodes.get(node, {})}
        self.nodes[node] = merged
        self._alias[(etype, eid)] = node
        for value in merged.values():
            self._alias.setdefault((etype, str(value)), node)
        return node

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def resolve(self, etype: str, eid: str) -> Node | None:
        return self._alias.get((etype, str(eid)))

    def aliases(self, node: Node) -> list[str]:
        return [node[1], *sorted({v for v in self.nodes.get(node, {}).values() if v and v != node[1]})]

    def walk(self, start: Node, max_hops: int) -> dict[Node, list[Node]]:
        """Every node within max_hops of start -> the path start..node."""
        paths = {start: [start]}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            if len(paths[current]) > max_hops:
                continue
            for nxt in sorted(self.adjacent.get(current, ())):
                if nxt not in paths:
                    paths[nxt] = [*paths[current], nxt]
                    queue.append(nxt)
        return paths

    def depends_on(self, dependent: Node, dependency: Node, max_hops: int) -> bool:
        """True when `dependent` reaches `dependency` along depends edges."""
        seen = {dependent}
        frontier = [dependent]
        for _ in range(max_hops):
            nxt = []
            for node in frontier:
                for dep in self.depends.get(node, ()):
                    if dep == dependency:
                        return True
                    if dep not in seen:
                        seen.add(dep)
                        nxt.append(dep)
            frontier = nxt
        return False


# ------------------------------------------------------------------- requests

def _anchor_rows(finding: AgentFinding) -> list[dict[str, Any]]:
    return [r for r in finding.evidence if r.get("entity_type") and r.get("entity_id")]


def _covered(peers: Iterable[AgentFinding], domain: str, graph: TopologyGraph, node: Node) -> bool:
    """A peer domain already reports rule-fired evidence on this entity."""
    for peer in peers:
        if peer.domain != domain:
            continue
        for row in peer.evidence:
            if graph.resolve(row["entity_type"], row["entity_id"]) == node:
                return True
    return False


def request_id(target: str, node: Node, start: datetime, end: datetime) -> str:
    raw = f"{target}|{node_key(node)}|{start.isoformat()}|{end.isoformat()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def plan_requests(
    domain_id: str,
    finding: AgentFinding | None,
    peers: list[AgentFinding],
    topology: TopologyResponse | dict | None,
    catalog: Catalog,
    *,
    window_start: datetime,
    window_end: datetime,
    alarm_entities: list[dict] | None = None,
    leads: list[dict] | None = None,
    asked: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """The validation_requests one agent sends this round.

    Anchors are the entities of this agent's own evidence, any leads it was
    handed, and the alarm's entities when this domain can see their type.
    From each anchor the agent walks the topology and, for every
    entity within `walk.max_hops` that the anchor depends on (topology
    `dependent` direction), asks each other domain that can see that entity
    type (`probes`), unless that domain already has evidence on it or was
    already asked. Nearer entities are asked first; at most
    `walk.max_requests_per_round`.
    """
    graph = TopologyGraph.build(topology, catalog)
    if not graph:
        return []
    spec = catalog.investigation
    lookback = timedelta(minutes=spec.walk.lookback_minutes)
    asked = set(asked)

    anchors: list[tuple[Node, datetime, list[str], str]] = []  # node, first_seen, signals, origin
    for row in _anchor_rows(finding) if finding is not None and finding.anomaly_detected else []:
        node = graph.resolve(row["entity_type"], row["entity_id"])
        if node is not None:
            anchors.append((node, _ts(row["first_seen"]), list(row.get("signals", [])), "evidence"))
    for lead in leads or []:
        node = graph.resolve(lead["type"], lead["id"])
        if node is not None:
            anchors.append((node, _ts(lead.get("seen_at") or window_end), [], "lead"))
    anchored = {a[0] for a in anchors}
    for ent in alarm_entities or []:
        node = graph.resolve(str(ent.get("type")), str(ent.get("id")))
        if node is not None and node not in anchored and domain_id in spec.probes.get(node[0], []):
            anchors.append((node, window_end, [], "alarm"))

    candidates: dict[tuple[str, Node], dict[str, Any]] = {}
    for anchor, first_seen, signals, origin in anchors:
        start = min(first_seen, window_start) - lookback
        for node, path in graph.walk(anchor, spec.walk.max_hops).items():
            hops = len(path) - 1
            # Ask about the anchor itself and about what it depends on; an
            # entity that merely shares a neighbor cannot explain the anchor.
            if hops and not graph.depends_on(anchor, node, spec.walk.max_hops):
                continue
            for target in spec.probes.get(node[0], []):
                if target == domain_id or target not in catalog.domains.domains:
                    continue
                if _covered(peers, target, graph, node):
                    continue
                rid = request_id(target, node, start, window_end)
                if rid in asked:
                    continue
                priority = (hops, 0)
                known = candidates.get((target, node))
                if known is not None and known["_priority"] <= priority:
                    continue
                what = f" ({', '.join(signals)})" if signals else ""
                candidates[(target, node)] = {
                    "_priority": priority,
                    "request_id": rid,
                    "target": target,
                    "entity": {"type": node[0], "id": node[1]},
                    "entity_filters": [{"type": node[0], "id": alias} for alias in graph.aliases(node)],
                    "start_time": start.isoformat(),
                    "end_time": window_end.isoformat(),
                    "anchor": {"type": anchor[0], "id": anchor[1], "origin": origin},
                    "path": [node_key(n) for n in path],
                    "hops": hops,
                    "question": (
                        f"{domain_id} saw {node_key(anchor)}{what}"
                        + (f"; {node_key(node)} is {hops} hop(s) away via {' -> '.join(node_key(n) for n in path)}" if hops else "")
                        + f". Any {target} evidence on {node_key(node)} between {start:%H:%MZ} and {window_end:%H:%MZ}?"
                    ),
                }
    ordered = sorted(candidates.values(), key=lambda c: (c["_priority"], c["target"], c["entity"]["type"], c["entity"]["id"]))
    out = []
    for c in ordered[: spec.walk.max_requests_per_round]:
        c.pop("_priority")
        out.append(c)
    return out


# -------------------------------------------------------------------- answers

def verdict(finding: AgentFinding) -> str:
    if finding.data_degraded and not finding.anomaly_detected:
        return "inconclusive"
    return "supported" if finding.anomaly_detected and finding.evidence else "refuted"


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def find_leads(finding: AgentFinding, catalog: Catalog, exclude: Iterable[tuple[str, str]] = ()) -> list[dict[str, str]]:
    """Entities a scoped answer names that were not asked about: the entities
    of its evidence, and ids that `lead_patterns` find in its key events
    (e.g. the route table inside a CloudTrail request)."""
    exclude = set(exclude)
    leads: dict[tuple[str, str], dict[str, str]] = {}
    for row in finding.evidence:
        key = (row["entity_type"], row["entity_id"])
        if key not in exclude:
            leads.setdefault(key, {"type": key[0], "id": key[1], "seen_at": row.get("first_seen", "")})
    patterns = [(re.compile(p), t) for p, t in catalog.investigation.lead_patterns.items()]
    for event in finding.key_events:
        if not event.get("rules_fired"):
            continue
        seen_at = str(event.get("timestamp", ""))
        for text in _strings({k: v for k, v in event.items() if k not in ("rules_fired", "evidence_refs")}):
            for pattern, etype in patterns:
                for match in pattern.findall(text):
                    key = (etype, match)
                    if key not in exclude:
                        leads.setdefault(key, {"type": etype, "id": match, "seen_at": seen_at})
    return list(leads.values())


def absorb(finding: AgentFinding, answers: list[tuple[dict[str, Any], dict[str, Any]]], catalog: Catalog,
           limit: int | None = None) -> AgentFinding:
    """Revise one domain's finding once with the supported answers it gave
    this round: their evidence joins `evidence` (via='validation'), their key
    events are added, and a clean finding becomes anomalous. Idempotent: an
    answer whose evidence is already present changes nothing."""
    revised = finding.model_copy(deep=True)
    known = {(r["entity_type"], r["entity_id"], tuple(r.get("signals", []))) for r in revised.evidence}
    known_refs = {ref for e in revised.key_events for ref in e.get("evidence_refs", [])}
    notes = []
    for request, answer in answers:
        scoped = AgentFinding.model_validate(answer["finding"])
        new_rows = [r for r in scoped.evidence if (r["entity_type"], r["entity_id"], tuple(r.get("signals", []))) not in known]
        if not new_rows:
            continue
        for row in new_rows:
            known.add((row["entity_type"], row["entity_id"], tuple(row.get("signals", []))))
            revised.evidence.append({**row, "via": "validation", "requested_by": request.get("sender"),
                                     "request_id": request["request_id"]})
        for event in scoped.key_events:
            if event.get("rules_fired") and not set(event.get("evidence_refs", [])) & known_refs:
                revised.key_events.append(event)
                known_refs.update(event.get("evidence_refs", []))
        notes.append(f"{request.get('sender')} asked about {node_key((request['entity']['type'], request['entity']['id']))}: "
                     f"{', '.join(sorted({s for r in new_rows for s in r.get('signals', [])}))}")
        if not revised.anomaly_detected:
            revised.anomaly_detected = True
            revised.confidence = scoped.confidence
            revised.start_time, revised.end_time = scoped.start_time, scoped.end_time
        else:
            revised.confidence = max(revised.confidence, scoped.confidence)
            revised.start_time = min(revised.start_time, scoped.start_time)
    if not notes:
        return finding
    revised.key_events.sort(key=lambda e: 0 if any(catalog.signal_role(r["signal"]) == "cause" for r in e.get("rules_fired", [])) else 1)
    if limit is not None:
        revised.key_events = revised.key_events[: max(limit, 1) + len(notes)]
    revised.summary = f"{revised.summary}. Peer validation: " + "; ".join(notes)
    revised.revised = True
    revised.revision_count += 1
    return revised


# -------------------------------------------------------------------- ranking

def _targets(findings: list[AgentFinding], graph: TopologyGraph, alarm_entities: list[dict], catalog: Catalog) -> set[Node]:
    """What the causes are ranked against: the alarm's entities, or, without
    them, the entities showing symptoms."""
    nodes = {graph.resolve(str(e.get("type")), str(e.get("id"))) for e in alarm_entities}
    nodes.discard(None)
    if nodes:
        return nodes  # type: ignore[return-value]
    for f in findings:
        for row in f.evidence:
            if row.get("role") == "symptom":
                node = graph.resolve(row["entity_type"], row["entity_id"])
                if node is not None:
                    nodes.add(node)
    return nodes  # type: ignore[return-value]


# A cause shortly before the first symptom gets full precedence; older ones
# fade (a change 25 minutes earlier is more likely unrelated background), and
# anything clearly after the symptoms gets none. Metric datapoints are stamped
# at the start of their period and logs arrive later, so two minutes of slack.
PRECEDENCE_FULL_SECONDS = 300
PRECEDENCE_SLACK_SECONDS = 120


def _precedence(first_seen: datetime, onset: datetime) -> float:
    before = (onset - first_seen).total_seconds()
    if before < -PRECEDENCE_SLACK_SECONDS:
        return 0.0
    if before <= PRECEDENCE_FULL_SECONDS:
        return 1.0
    return round(PRECEDENCE_FULL_SECONDS / before, 3)


def rank_causes(findings: list[AgentFinding], topology: TopologyResponse | dict | None, catalog: Catalog,
                alarm_entities: list[dict] | None = None) -> list[RankedCause]:
    """Every entity with rule-fired evidence that sits in the topology,
    best-first. Empty without topology (or when no evidence entity is in it),
    which leaves the synthesizer's per-domain path unchanged."""
    graph = TopologyGraph.build(topology, catalog)
    if not graph:
        return []
    spec = catalog.investigation
    weights = spec.rank_weights
    targets = _targets(findings, graph, alarm_entities or [], catalog)

    grouped: dict[tuple[Node, str], dict[str, Any]] = {}
    for f in findings:
        for row in f.evidence:
            node = graph.resolve(row["entity_type"], row["entity_id"])
            if node is None:
                continue
            g = grouped.setdefault((node, f.domain), {"rows": [], "confidence": f.confidence})
            g["rows"].append(row)
    if not grouped:
        return []

    # When the current trouble started: for each alarmed entity, the onset of
    # its latest bad episode (its most recent row); the incident's onset is the
    # earliest of those. Older blips on the same entities don't count.
    def latest(rows: list[dict[str, Any]]) -> datetime:
        return max(_ts(r.get("onset") or r["first_seen"]) for r in rows)

    per_target = [latest(g["rows"]) for (node, _), g in grouped.items() if node in targets]
    if not per_target:
        per_target = [latest([r]) for g in grouped.values() for r in g["rows"] if r.get("role") == "symptom"]
    onset = min(per_target) if per_target else None

    ranked: list[RankedCause] = []
    for (node, domain), g in grouped.items():
        rows = g["rows"]
        signals = sorted({s for r in rows for s in r.get("signals", [])})
        first_seen = min(_ts(r["first_seen"]) for r in rows)
        began = latest(rows)  # this candidate's latest episode, as for the alarmed entities
        roles = {r.get("role") for r in rows}
        role = "cause" if "cause" in roles else (None if None in roles else "symptom")
        severity = max(SEVERITY_SCORE.get(r.get("severity", "info"), 0.25) for r in rows)
        # Distinct signals count up to two: a deploy touching one table with
        # five API calls is not five times the evidence.
        evidence = min(1.0, 0.5 * severity + 0.15 * min(len(signals), 2) + 0.25 * g["confidence"])

        chain: list[str] = []
        if not targets:
            topo = 0.5
        elif node in targets:
            topo = 0.3
        else:
            topo = 0.0
            best = None
            for t in targets:
                path = graph.walk(t, spec.walk.max_hops).get(node)
                if path is not None and (best is None or len(path) < len(best)):
                    best = path
                if graph.depends_on(t, node, spec.walk.max_hops):
                    topo = 1.0
            if best is not None:
                chain = [node_key(n) for n in reversed(best)]
                topo = max(topo, 0.6)

        precedence = _precedence(began, onset) if onset is not None else 0.5

        features = {"evidence": round(evidence, 3), "topology": topo, "precedence": precedence,
                    "role": ROLE_SCORE[role]}
        total = weights.evidence + weights.topology + weights.precedence + weights.role or 1.0
        score = (weights.evidence * features["evidence"] + weights.topology * features["topology"]
                 + weights.precedence * features["precedence"] + weights.role * features["role"]) / total
        refs = [ref for r in rows for ref in r.get("evidence_refs", [])]
        ranked.append(RankedCause(
            id=f"C{len(ranked) + 1}", entity_type=node[0], entity_id=node[1], domain=domain, signals=signals,
            evidence_refs=list(dict.fromkeys(refs))[:10], first_seen=first_seen, score=round(score, 3),
            features=features, chain=chain, via_validation=any(r.get("via") == "validation" for r in rows),
            aliases=graph.aliases(node)[1:],
        ))
    ranked.sort(key=lambda c: (-c.score, c.first_seen, c.entity_type, c.entity_id))
    return [c.model_copy(update={"id": f"C{i + 1}"}) for i, c in enumerate(ranked)]


# --------------------------------------------------------------------- status

def incident_status(findings: list[AgentFinding], flap_episodes: int = 2) -> str:
    """flapping: an entity went bad `flap_episodes` or more separate times;
    ongoing: an entity is still bad at its last sample; recovered: every
    entity with a state has cleared. Entities seen only through events (log
    lines) have no state and do not count."""
    rows = [r for f in findings if f.anomaly_detected for r in f.entity_status]
    if any(r.get("episodes", 0) >= flap_episodes for r in rows):
        return "flapping"
    states = {r.get("current_state") for r in rows} - {"unknown"}
    if "firing" in states:
        return "ongoing"
    return "recovered" if states else "unknown"
