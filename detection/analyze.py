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
     and confidence from the domain's templates
  6. never report a degraded domain as clean
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from contract import Entity, Signal, ToolResponse
from contract.catalog_loader import Catalog
from contract.catalog_models import DomainDef, SummaryVariant, rule_id
from detection.relevance import is_relevant
from detection.rules import BaselineLookup, RuleContext, RuleHit, evaluate, series_key
from detection.templates import render
from models.schemas import AgentFinding

logger = logging.getLogger("net_cortex.detection")

MAX_UNCLASSIFIED = 20


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
) -> AgentFinding:
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
            if relevance is not None and cat_name in relevance.applies_to and not is_relevant(relevance, signal, incident_description):
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
    key_events = [events[id(o)] for o in ke_source[: domain.key_events.limit]]

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
    )
