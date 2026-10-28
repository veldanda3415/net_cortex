"""Generic, catalog-driven domain analysis: ToolResponse -> AgentFinding.

Pure function of (catalog, domain, response, incident context, baseline
lookup). Every domain agent runs this same code; what differs per domain
lives in domains.json / signals.json.

Pipeline:
  1. classify signals against the catalog (unknown -> unclassified evidence)
  2. group into observations (one per signal, or per entity+timestamp)
  3. evaluate each signal's rule; apply the optional relevance filter
  4. derive per-entity counts and evaluate their rules
  5. build key events (with the rule that fired + evidence_ref), summary
     and confidence from the domain's templates. Repeats of the same line
     on the same entity collapse into one key event with a `count`, and
     cause signals (investigation.json `signal_roles`) go first, so a burst
     of generic symptoms cannot crowd out the decisive line. `evidence`
     keeps every hit (deduplicated the same way), not just the key events.
  6. never report a degraded domain as clean
  7. per hit entity: still firing or cleared, and how often it flipped
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from contract import Entity, Signal, ToolResponse
from contract.catalog_loader import Catalog
from contract.catalog_models import DomainDef, SummaryVariant, rule_id
from detection.relevance import is_relevant
from detection.rules import SEVERITY_RANK, BaselineLookup, RuleContext, RuleHit, evaluate, series_key
from detection.templates import render
from models.schemas import AgentFinding

logger = logging.getLogger("net_cortex.detection")

MAX_UNCLASSIFIED = 20
MAX_EVIDENCE = 50
MAX_REFS_PER_ROW = 5
ROLE_RANK = {"cause": 0, None: 1, "symptom": 2}
_DIGITS = re.compile(r"\d+")


@dataclass
class _Hit:
    signal: Signal
    catalog_name: str
    hit: RuleHit


@dataclass
class _Observation:
    signals: list[Signal] = field(default_factory=list)
    catalog_names: list[str] = field(default_factory=list)
    candidate: bool = False
    hits: list[_Hit] = field(default_factory=list)

    @property
    def timestamp(self) -> datetime:
        return self.signals[0].timestamp

    @property
    def entity(self) -> Entity:
        return self.signals[0].entity


@dataclass
class _Derived:
    entity: Entity
    hit: RuleHit


def _group_key(signal: Signal) -> tuple:
    return (
        signal.timestamp,
        signal.entity.type,
        signal.entity.id,
        tuple(signal.entity.labels.items()),
        signal.attributes.get("region"),
    )


def _text_key(obs: _Observation) -> str:
    """A log line's text with numbers blanked, so the same message repeated
    with new timestamps/counters/IPs counts as one line."""
    for s in obs.signals:
        for attr in ("message", "sample", "details"):
            if attr in s.attributes:
                return _DIGITS.sub("#", str(s.attributes[attr]))
    return ""


def _dedupe_key(obs: _Observation) -> tuple:
    names = sorted({h.signal.name for h in obs.hits}) if obs.hits else sorted(set(obs.catalog_names))
    return (bool(obs.hits), tuple(names), obs.entity.type, obs.entity.id,
            tuple(sorted(obs.entity.labels.items())), _text_key(obs))


def _role(catalog: Catalog, names: list[str]) -> str | None:
    roles = {catalog.signal_role(n) for n in names}
    if "cause" in roles:
        return "cause"
    return "symptom" if "symptom" in roles else None


def _obs_role(catalog: Catalog, obs: _Observation) -> str | None:
    names = [h.signal.name for h in obs.hits] + [h.catalog_name for h in obs.hits] if obs.hits else []
    return _role(catalog, names)


def _evidence(catalog: Catalog, hit_obs: list[_Observation], events: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[tuple, dict[str, Any]] = {}
    for obs in hit_obs:
        key = _dedupe_key(obs)
        ts = obs.timestamp.isoformat()
        severity = max((h.signal.severity for h in obs.hits), key=lambda sev: SEVERITY_RANK[sev])
        row = rows.get(key)
        if row is None:
            names = sorted({h.signal.name for h in obs.hits})
            row = rows[key] = {
                "entity_type": obs.entity.type, "entity_id": obs.entity.id, "signals": names,
                "role": _obs_role(catalog, obs), "severity": severity, "first_seen": ts, "last_seen": ts,
                "count": 0, "rules_fired": events[id(obs)]["rules_fired"], "evidence_refs": [],
            }
        row["count"] += 1
        row["first_seen"] = min(row["first_seen"], ts)
        row["last_seen"] = max(row["last_seen"], ts)
        if SEVERITY_RANK[severity] > SEVERITY_RANK[row["severity"]]:
            row["severity"] = severity
        for h in obs.hits:
            ref = h.signal.evidence_ref
            if ref and ref not in row["evidence_refs"] and len(row["evidence_refs"]) < MAX_REFS_PER_ROW:
                row["evidence_refs"].append(ref)
    ordered = sorted(rows.values(), key=lambda r: (ROLE_RANK[r["role"]], -SEVERITY_RANK[r["severity"]], r["first_seen"]))
    return ordered[:MAX_EVIDENCE]


def _with_onsets(rows: list[dict[str, Any]], hit_obs: list[_Observation], firing: dict[tuple, list[tuple[datetime, bool]]],
                 stateful: set[tuple]) -> list[dict[str, Any]]:
    """`onset`: when the row's latest bad episode began. For a metric that dipped
    at the start of the window and again at the alarm, that is the second dip,
    not first_seen; ranking compares causes against it. Events keep first_seen."""
    cats: dict[tuple[str, str, str], set[str]] = {}
    for obs in hit_obs:
        for h in obs.hits:
            cats.setdefault((obs.entity.type, obs.entity.id, h.signal.name), set()).add(h.catalog_name)
    for row in rows:
        onset = None
        for name in row["signals"]:
            for cat in cats.get((row["entity_type"], row["entity_id"], name), ()):
                key = (row["entity_type"], row["entity_id"], cat)
                if key not in stateful:
                    continue
                ordered = sorted(firing.get(key, []), key=lambda item: item[0])
                edges = [t for i, (t, hit) in enumerate(ordered) if hit and (i == 0 or not ordered[i - 1][1])]
                if edges:
                    candidate = edges[-1].isoformat()
                    onset = candidate if onset is None else max(onset, candidate)
        row["onset"] = onset or row["first_seen"]
    return rows


def _entity_status(hit_obs: list[_Observation], firing: dict[tuple, list[tuple[datetime, bool]]],
                   stateful: set[tuple]) -> list[dict[str, Any]]:
    """For each hit entity, over every sample seen (window + post-alarm):
    `episodes`, the most separate times one of its series went bad, and
    `current_state` from the latest sample of its stateful series (metric or
    state; an event series has no "cleared" sample, so an entity seen only
    through events is `unknown`)."""
    out = []
    seen: set[tuple[str, str]] = set()
    for obs in hit_obs:
        ent = (obs.entity.type, obs.entity.id)
        if ent in seen:
            continue
        seen.add(ent)
        episodes = 0
        transitions = 0
        last: list[tuple[datetime, bool]] = []
        for key, samples in firing.items():
            if key[:2] != ent:
                continue
            ordered = sorted(samples, key=lambda item: item[0])
            states = [hit for _, hit in ordered]
            episodes = max(episodes, sum(1 for i, hit in enumerate(states) if hit and (i == 0 or not states[i - 1])))
            if key in stateful:
                transitions += sum(1 for a, b in zip(states, states[1:]) if a != b)
                last.append(ordered[-1])
        if last:
            current = "firing" if max(last, key=lambda item: item[0])[1] else "cleared"
        else:
            current = "unknown"
        out.append({"entity_type": ent[0], "entity_id": ent[1], "current_state": current,
                    "episodes": episodes, "transitions": transitions})
    return out


def _unclassified_row(signal: Signal) -> dict[str, Any]:
    return {
        "name": signal.name,
        "signal_type": signal.signal_type,
        "source": signal.source,
        "entity": f"{signal.entity.type}:{signal.entity.id}",
        "value": signal.value,
        "severity": signal.severity,
        "timestamp": signal.timestamp.isoformat(),
        "evidence_ref": signal.evidence_ref,
    }


def key_event(obs: _Observation, domain: DomainDef) -> dict[str, Any]:
    """A self-describing key event: legacy-shaped fields first (so text and
    downstream readers keep working), then the rule(s) that fired and the
    evidence_ref(s) that make every claim traceable."""
    entity = obs.entity
    event: dict[str, Any] = {"timestamp": obs.timestamp}
    if domain.entity_field:
        event[domain.entity_field] = entity.id
    for s in obs.signals:
        if s.value is not None:
            event[s.name] = s.value
    for s in obs.signals:
        for k, v in s.attributes.items():
            event.setdefault(k, v)
    if entity.labels:
        event["tags"] = dict(entity.labels)
    event["entity_type"] = entity.type
    event["entity_id"] = entity.id
    if len(obs.signals) == 1:
        event["name"] = obs.signals[0].name
        event["severity"] = obs.signals[0].severity
    event["rules_fired"] = [
        {"signal": h.signal.name, "rule": h.hit.rule_id, "reason": h.hit.reason, "evidence_ref": h.signal.evidence_ref}
        for h in obs.hits
    ]
    event["evidence_refs"] = [s.evidence_ref for s in obs.signals if s.evidence_ref]
    return event


def _template_view(obs: _Observation, event: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    for h in obs.hits:
        if h.hit.reason not in reasons:
            reasons.append(h.hit.reason)
    return {**event, "reasons": ", ".join(reasons)}


def _variant_matches(variant: SummaryVariant, hit_names: set[str], candidate_count: int, derived: list[_Derived]) -> bool:
    w = variant.when
    if w is None:
        return True
    if w.hit_names_any is not None and not (hit_names & set(w.hit_names_any)):
        return False
    if w.candidates_present is not None and (candidate_count > 0) != w.candidates_present:
        return False
    if w.derived_hits is not None and bool(derived) != w.derived_hits:
        return False
    return True


def _pick(variants: list[SummaryVariant], *args) -> list[SummaryVariant]:
    return [v for v in variants if _variant_matches(v, *args)]


def _coverage_problem(response: ToolResponse) -> str:
    problems = [f"{c.source}: {c.error or c.status}" for c in response.coverage if c.status != "ok"]
    return "; ".join(problems)[:300] or "no source reported coverage"


def analyze_domain(
    catalog: Catalog,
    domain_id: str,
    response: ToolResponse,
    *,
    region: str,
    incident_description: str = "",
    baseline_lookup: BaselineLookup | None = None,
    legacy_fallback: bool = True,
    now: datetime | None = None,
    scoped: bool = False,
    after: list[Signal] | None = None,
) -> AgentFinding:
    """`scoped`: the query was narrowed to entities a peer asked about, so the
    incident-text relevance filter (meant for region-wide sweeps) is skipped.
    `after`: samples from after the window (alarm -> now); they never create
    hits, they only tell whether a hit entity is still firing."""
    domain = catalog.domain(domain_id)
    window_start = response.window.start

    # 1. classify --------------------------------------------------------------
    current: list[tuple[Signal, str]] = []
    baseline_samples: list[Signal] = []
    unclassified: list[Signal] = []
    for signal in response.signals:
        found = catalog.signal(signal.name)
        if found is None:
            unclassified.append(signal)
            continue
        cat_name, sig_def = found
        if sig_def.domain != domain_id:
            logger.debug("domain=%s ignoring signal %s owned by domain %s", domain_id, signal.name, sig_def.domain)
            continue
        if signal.timestamp < window_start:
            baseline_samples.append(signal)
        else:
            current.append((signal, cat_name))
    if unclassified:
        names = sorted({s.name for s in unclassified})
        logger.info("domain=%s kept %s unclassified signal(s) not in the catalog: %s",
                    domain_id, len(unclassified), ", ".join(names[:10]))

    ctx = RuleContext(region=region, baseline_lookup=baseline_lookup, legacy_fallback=legacy_fallback)
    for signal, _ in sorted(current, key=lambda item: item[0].timestamp):
        ctx.series.setdefault(series_key(signal), []).append(signal)
    for signal in sorted(baseline_samples, key=lambda s: s.timestamp):
        ctx.baseline_series.setdefault(series_key(signal), []).append(signal)

    # 2. observations ----------------------------------------------------------
    observations: list[_Observation] = []
    grouped: dict[tuple, _Observation] = {}
    for signal, cat_name in current:
        if domain.observation == "entity_time":
            key = _group_key(signal)
            obs = grouped.get(key)
            if obs is None:
                obs = grouped[key] = _Observation()
                observations.append(obs)
        else:
            obs = _Observation()
            observations.append(obs)
        obs.signals.append(signal)
        obs.catalog_names.append(cat_name)

    # 3. rules + relevance -----------------------------------------------------
    relevance = domain.relevance
    for obs in observations:
        for signal, cat_name in zip(obs.signals, obs.catalog_names):
            sig_def = catalog.signals.signals[cat_name]
            if sig_def.rule is None:
                continue
            obs.candidate = True
            names = [cat_name, *sig_def.match_names]
            hit = evaluate(sig_def.rule, signal, ctx, rule_id=rule_id(cat_name, sig_def.rule), catalog_names=names)
            if hit is None:
                continue
            if (relevance is not None and not scoped and cat_name in relevance.applies_to
                    and not is_relevant(relevance, signal, incident_description)):
                continue
            obs.hits.append(_Hit(signal, cat_name, hit))
    hit_obs = [o for o in observations if o.hits]
    candidate_count = sum(1 for o in observations if o.candidate)

    # 4. derived per-entity counts ----------------------------------------------
    derived: list[_Derived] = []
    for spec in domain.derived:
        sig_def = catalog.signals.signals[spec.signal]
        counts: dict[tuple[str, str], tuple[Entity, int, Signal]] = {}
        for obs in hit_obs:
            key = (obs.entity.type, obs.entity.id)
            entity, n, sample = counts.get(key, (obs.entity, 0, obs.signals[0]))
            counts[key] = (entity, n + 1, sample)
        for entity, n, sample in counts.values():
            synthetic = Signal(
                signal_type="metric", timestamp=sample.timestamp, observed_at=sample.observed_at,
                entity=Entity(type=entity.type, id=entity.id, labels=entity.labels),
                name=spec.signal, value=float(n), domain=domain.telemetry_domain, source="derived",
                attributes={"region": sample.attributes.get("region", region)},
            )
            if sig_def.rule is None:
                continue
            hit = evaluate(sig_def.rule, synthetic, ctx, rule_id=rule_id(spec.signal, sig_def.rule), catalog_names=[spec.signal])
            if hit is not None:
                derived.append(_Derived(entity, hit))

    # 5. finding ---------------------------------------------------------------
    anomaly = bool(hit_obs)
    events = {id(o): key_event(o, domain) for o in observations}
    if domain.key_events.source == "all":
        ke_source = observations
    elif domain.key_events.source == "hits":
        ke_source = hit_obs
    else:
        ke_source = hit_obs if anomaly else observations
    deduped: dict[tuple, _Observation] = {}
    counts: dict[tuple, int] = {}
    for o in ke_source:
        key = _dedupe_key(o)
        deduped.setdefault(key, o)
        counts[key] = counts.get(key, 0) + 1
    ke_obs = sorted(deduped.values(), key=lambda o: ROLE_RANK[_obs_role(catalog, o)])
    key_events = []
    for o in ke_obs[: domain.key_events.limit]:
        event = events[id(o)]
        n = counts[_dedupe_key(o)]
        key_events.append({**event, "count": n} if n > 1 else event)

    # 7. firing state per (entity, signal) series, window + after ---------------
    firing: dict[tuple, list[tuple[datetime, bool]]] = {}
    stateful: set[tuple] = set()
    hit_entities = {(o.entity.type, o.entity.id) for o in hit_obs}
    for o in observations:
        if (o.entity.type, o.entity.id) not in hit_entities:
            continue
        hit_names = {h.catalog_name for h in o.hits}
        for signal, cat_name in zip(o.signals, o.catalog_names):
            key = (o.entity.type, o.entity.id, cat_name)
            firing.setdefault(key, []).append((o.timestamp, cat_name in hit_names))
            if signal.signal_type != "event":
                stateful.add(key)
    for signal in after or []:
        if (signal.entity.type, signal.entity.id) not in hit_entities:
            continue
        found = catalog.signal(signal.name)
        if found is None or found[1].domain != domain_id or found[1].rule is None:
            continue
        cat_name, sig_def = found
        hit = evaluate(sig_def.rule, signal, ctx, rule_id=rule_id(cat_name, sig_def.rule),
                       catalog_names=[cat_name, *sig_def.match_names])
        key = (signal.entity.type, signal.entity.id, cat_name)
        firing.setdefault(key, []).append((signal.timestamp, hit is not None))
        if signal.signal_type != "event":
            stateful.add(key)

    hit_names = {h.catalog_name for o in hit_obs for h in o.hits} | {h.signal.name for o in hit_obs for h in o.hits}
    top_derived = max(derived, key=lambda d: d.hit.z or 0.0) if derived else None
    view: dict[str, Any] = {
        "domain": domain_id,
        "hit_count": len(hit_obs),
        "candidate_count": candidate_count,
        "observation_count": len(observations),
        "entity_count": len({o.entity.id for o in hit_obs}),
        "derived": (
            {"entity_id": top_derived.entity.id, "entity_type": top_derived.entity.type, "z": top_derived.hit.z,
             "reason": top_derived.hit.reason}
            if top_derived else {}
        ),
    }
    if hit_obs:
        tally: dict[str, int] = {}
        for o in hit_obs:
            tally[o.entity.id] = tally.get(o.entity.id, 0) + 1
        view["top_entity"] = max(tally, key=tally.get)
        view["first"] = _template_view(hit_obs[0], events[id(hit_obs[0])])
        latest = max(hit_obs, key=lambda o: o.timestamp)
        view["latest"] = _template_view(latest, events[id(latest)])

        def rank(o: _Observation) -> float:
            total = 0.0
            for f in domain.rank_by:
                try:
                    total += float(events[id(o)].get(f, 0) or 0)
                except (TypeError, ValueError):
                    pass
            return total

        worst = max(hit_obs, key=rank)
        view["worst"] = _template_view(worst, events[id(worst)])

    match_args = (hit_names, candidate_count, derived)
    if anomaly:
        summary = render(_pick(domain.summary.anomaly, *match_args)[0].template, view)
        for suffix in _pick(domain.summary.anomaly_suffixes, *match_args):
            summary += render(suffix.template, view)
        conf_spec = domain.confidence.anomaly
        max_z = max((d.hit.z or 0.0 for d in derived), default=0.0)
        confidence = min(conf_spec.max, conf_spec.base + conf_spec.per_hit * len(hit_obs) + conf_spec.derived_z_weight * max_z)
    else:
        summary = render(_pick(domain.summary.clean, *match_args)[0].template, view)
        confidence = domain.confidence.clean

    # 6. degraded data is never reported as clean ---------------------------------
    if response.degraded:
        problem = _coverage_problem(response)
        if anomaly:
            summary = f"{summary}; telemetry partially unavailable ({problem})"
        else:
            summary = (
                f"Telemetry incomplete for this window ({problem}); no anomaly in the data that was "
                "retrieved, so this domain is NOT verified clean"
            )

    if domain.time_range == "hits_else_all" and hit_obs:
        time_source = hit_obs
    else:
        time_source = observations
    fallback_time = now or datetime.now(timezone.utc)
    return AgentFinding(
        agent_id=domain_id,
        domain=domain_id,
        anomaly_detected=anomaly,
        summary=summary,
        key_events=key_events,
        start_time=min((o.timestamp for o in time_source), default=fallback_time),
        end_time=max((o.timestamp for o in time_source), default=fallback_time),
        confidence=confidence,
        data_degraded=response.degraded,
        unclassified_evidence=[_unclassified_row(s) for s in unclassified[:MAX_UNCLASSIFIED]],
        evidence=_with_onsets(_evidence(catalog, hit_obs, events), hit_obs, firing, stateful),
        entity_status=_entity_status(hit_obs, firing, stateful),
    )
