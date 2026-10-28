"""Typed models for the detection catalog (contract/catalog/*.json).

The catalog is data: which signals exist, how each is detected, which
domains run and how findings corroborate each other. Everything here is a
closed vocabulary — rule types, condition types and template placeholders
are fixed sets interpreted by detection/, never evaluated as expressions.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SEVERITIES = ("debug", "info", "warning", "error", "critical")
Severity = Literal["debug", "info", "warning", "error", "critical"]

# MCP tools the telemetry server implements. Adding one is a code change.
KNOWN_TOOLS: dict[str, str | None] = {
    "get_metrics": "metrics",
    "get_logs": "logs",
    "get_routing_events": "routing",
    "get_config_changes": "config",
    "get_signals": None,  # generic: serves any catalog domain
}


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------- entities

class EntityType(_M):
    description: str = ""


class Relationship(_M):
    description: str = ""
    source: list[str] = Field(min_length=1, description="Allowed source entity types")
    target: list[str] = Field(min_length=1, description="Allowed target entity types")
    dependent: Literal["source", "target", "both"] = Field(
        default="both",
        description="Which end depends on the other: a fault on the other end shows up on this end. "
        "Used to rank a cause upstream of the alarm above one that is only connected to it.",
    )


class EntitiesFile(_M):
    catalog_version: str = "1.0"
    entity_types: dict[str, EntityType] = Field(default_factory=dict)
    relationships: dict[str, Relationship] = Field(default_factory=dict)


# ----------------------------------------------------------------------- rules

class _Rule(_M):
    id: str | None = Field(default=None, description="Stable rule id; defaults to '<signal>.<type>'")
    reason: str | None = Field(default=None, description="Text recorded as evidence when the rule fires")


class ThresholdRule(_Rule):
    type: Literal["threshold"]
    op: Literal["gt", "lt", "between"]
    value: float | None = None
    min: float | None = None
    max: float | None = None
    field: str = Field(default="value", description="'value' or 'attributes.<name>'")
    per: str | None = Field(
        default=None,
        description="Divide the field by this one before comparing, e.g. 'labels.capacity_bps' for "
        "utilization. A signal without the divisor (missing or 0) does not fire.",
    )
    scale: float = Field(default=1.0, gt=0, description="Multiply the field by this first (e.g. 8 for bytes -> bits)")


class StateEqualsRule(_Rule):
    type: Literal["state_equals"]
    value: str | float | bool
    field: str = Field(default="value", description="'value' or 'attributes.<name>'")


class FallbackThreshold(ThresholdRule):
    pass


class ZScoreBaselineRule(_Rule):
    type: Literal["zscore_baseline"]
    threshold: float = Field(gt=0)
    baseline_metric: str | None = Field(default=None, description="Metric name in the baseline store; defaults to the signal name")
    baseline_keys: list[str] = Field(
        default_factory=lambda: ["region=region"],
        description="Ordered baseline lookup keys, each '<prefix>=<entity_id|entity_type|region|label:NAME>'",
    )
    min_baseline_samples: int = Field(default=3, ge=2, description="baseline_window samples needed to use them instead of the baseline provider")
    fallback: FallbackThreshold | None = Field(default=None, description="Used when no baseline exists and baselines.legacy_fallback is on")


class RateChangeRule(_Rule):
    type: Literal["rate_change"]
    direction: Literal["increase", "decrease"]
    factor: float = Field(gt=1, description="Fires when value >= reference*factor (increase) or <= reference/factor (decrease)")
    min_reference: float = Field(default=0.0, ge=0, description="Ignore series whose reference level is at or below this")
    reference: Literal["mean", "median"] = Field(
        default="mean", description="How the reference level is taken; median resists bursty series")


class EventPresenceRule(_Rule):
    type: Literal["event_presence"]
    names: list[str] | None = Field(default=None, description="Allow-list of signal names; defaults to the signal's name + match_names")
    min_severity: Severity = "info"


Rule = Annotated[
    Union[ThresholdRule, StateEqualsRule, ZScoreBaselineRule, RateChangeRule, EventPresenceRule],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------- signals

class SignalDef(_M):
    signal_type: Literal["metric", "event", "state"]
    domain: str = Field(description="Catalog domain id (domains.json) that analyzes this signal")
    unit: str | None = None
    description: str = Field(min_length=1, description="One line, passed to the LLM synthesizer")
    match_names: list[str] = Field(default_factory=list, description="Other raw names this definition covers")
    entity_types: list[str] = Field(default_factory=list, description="Expected entity types (informational)")
    derived: bool = Field(default=False, description="Computed by the domain agent (domains.json `derived`), not emitted by sources")
    rule: Rule | None = None


class SignalsFile(_M):
    catalog_version: str = "1.0"
    signals: dict[str, SignalDef] = Field(default_factory=dict)


# --------------------------------------------------------------------- domains

class SkillCard(_M):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)


class AgentCard(_M):
    name: str
    port: int = Field(ge=1, le=65535)
    description: str
    skill: SkillCard


class SummaryCondition(_M):
    hit_names_any: list[str] | None = None
    candidates_present: bool | None = None
    derived_hits: bool | None = None


class SummaryVariant(_M):
    when: SummaryCondition | None = None
    template: str


class SummarySpec(_M):
    anomaly: list[SummaryVariant] = Field(min_length=1)
    anomaly_suffixes: list[SummaryVariant] = Field(default_factory=list)
    clean: list[SummaryVariant] = Field(min_length=1)


class AnomalyConfidence(_M):
    base: float = Field(ge=0, le=1)
    per_hit: float = 0.0
    derived_z_weight: float = 0.0
    max: float = Field(default=0.95, ge=0, le=1)


class ConfidenceSpec(_M):
    anomaly: AnomalyConfidence
    clean: float = Field(ge=0, le=1)


class KeyEventsSpec(_M):
    source: Literal["hits", "all", "hits_else_all"] = "hits_else_all"
    limit: int = Field(default=5, ge=1)


class SymptomMatch(_M):
    field: str
    values: list[str]


class RelevanceSpec(_M):
    type: Literal["incident_text"]
    applies_to: list[str] = Field(min_length=1, description="Signal names the filter applies to; others always count")
    min_token_length: int = 4
    stopwords: list[str] = Field(default_factory=list)
    text_fields: list[str] = Field(min_length=1, description="'entity_id', 'name', or 'attributes.<name>'")
    symptom_terms: list[str] = Field(default_factory=list)
    symptom_match: SymptomMatch | None = None


class DerivedSpec(_M):
    signal: str = Field(description="Catalog signal (derived: true) to synthesize")
    count_of: Literal["hits"] = "hits"
    per: Literal["entity"] = "entity"


class DomainDef(_M):
    description: str = ""
    telemetry_domain: str = Field(description="Signal.domain value / telemetry endpoint domain")
    tool: str = Field(description="MCP tool that feeds this domain")
    agent: AgentCard
    observation: Literal["signal", "entity_time"] = "signal"
    entity_field: str | None = Field(default=None, description="Key under which key events carry entity.id (e.g. 'component')")
    rank_by: list[str] = Field(default_factory=list, description="Key-event fields summed to pick `worst`")
    key_events: KeyEventsSpec = Field(default_factory=KeyEventsSpec)
    time_range: Literal["all", "hits_else_all"] = "all"
    include_baseline: bool = False
    relevance: RelevanceSpec | None = None
    derived: list[DerivedSpec] = Field(default_factory=list)
    confidence: ConfidenceSpec
    summary: SummarySpec
    metrics_affected: list[str] = Field(default_factory=list)


class DomainsFile(_M):
    catalog_version: str = "1.0"
    domains: dict[str, DomainDef] = Field(default_factory=dict)
    degradation_map: dict[str, list[str]] = Field(default_factory=dict)


# ----------------------------------------------------------------- correlation

class PeerScope(_M):
    scope: Literal["self", "peers"]
    domains: list[str] | None = Field(default=None, description="Restrict peers to these domains")
    peer_anomaly: bool = Field(default=False, description="Only consider peers that detected an anomaly")


class KeyEventCondition(PeerScope):
    field: str
    op: Literal["eq", "lt", "gt", "contains"]
    value: str | float | bool


class SummaryContainsCondition(PeerScope):
    text: str


class PeerAnomalyCondition(_M):
    domains: list[str] | None = None


class Condition(_M):
    """Exactly one key set."""
    key_event: KeyEventCondition | None = None
    summary_contains: SummaryContainsCondition | None = None
    peer_anomaly: PeerAnomalyCondition | None = None
    any: list["Condition"] | None = None
    all: list["Condition"] | None = None


class When(_M):
    self_anomaly: bool
    all: list[Condition] = Field(default_factory=list)


class ConfidenceEffect(_M):
    op: Literal["at_least", "add"]
    value: float
    cap: float | None = Field(default=None, ge=0, le=1)


class Effect(_M):
    kind: Literal["corroboration", "contradiction"]
    confidence: ConfidenceEffect
    note: str = Field(description="Appended to the summary; may use {peer_anomaly_domains}")


class CorrelationRule(_M):
    domain: str
    priority: int = 100
    description: str = ""
    when: When
    effect: Effect


class CorrelationFile(_M):
    catalog_version: str = "1.0"
    rules: dict[str, CorrelationRule] = Field(default_factory=dict)


# --------------------------------------------------------------- investigation

class WalkSpec(_M):
    max_hops: int = Field(default=3, ge=1, le=6, description="Topology hops from an anchor entity")
    lookback_minutes: int = Field(default=15, ge=0, description="A question's window starts this long before the first symptom")
    max_requests_per_round: int = Field(default=12, ge=1, description="Cap on validation requests per collaboration round, across all agents (nearest first)")


class RankWeights(_M):
    evidence: float = Field(default=1.0, ge=0)
    topology: float = Field(default=1.0, ge=0)
    precedence: float = Field(default=1.0, ge=0)
    role: float = Field(default=1.0, ge=0)


class InvestigationFile(_M):
    catalog_version: str = "1.0"
    probes: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Entity type -> domains that can be asked about an entity of that type",
    )
    signal_roles: dict[str, Literal["cause", "symptom"]] = Field(
        default_factory=dict,
        description="Signal name (raw or catalog) -> role. A signal that is a cause in one fault and a "
        "symptom in another is left out; time precedence and topology decide it.",
    )
    role_with: dict[str, dict[str, Literal["cause", "symptom"]]] = Field(
        default_factory=dict,
        description="Signal -> {other signal: role}. When the other signal fires on the same entity at the same "
        "time, the first one takes this role instead, e.g. PrefixesSent=0 is only a symptom while BgpPeerState=0.",
    )
    lead_patterns: dict[str, str] = Field(
        default_factory=dict,
        description="Regex -> entity type. An agent answering a question proposes as new leads the ids these "
        "patterns find in its evidence (e.g. a route table id inside a CloudTrail request).",
    )
    walk: WalkSpec = Field(default_factory=WalkSpec)
    rank_weights: RankWeights = Field(default_factory=RankWeights)


# --------------------------------------------------------------------- sources

class EntityMapping(_M):
    type: str | None = None
    id_field: str | None = None
    label_field: str | None = None
    type_priority: list[str] = Field(default_factory=list, description="First label present becomes entity type/id")
    fallback_type: str | None = None
    fallback_id_field: str | None = None


class EmitSpec(_M):
    name: str
    value_field: str | None = None
    unit: str | None = None
    severity: Severity | None = None
    severity_field: str | None = None
    severity_map: dict[str, Severity] = Field(default_factory=dict)


class SourceMapping(_M):
    source: str
    record: str = Field(description="Raw record collection the plugin reads, e.g. 'metrics_data'")
    domain: str = Field(description="Telemetry domain the mapped signals belong to")
    signal_type: Literal["metric", "event", "state"]
    timestamp_field: str = "timestamp"
    region_field: str | None = Field(default=None, description="Per-record region; omit when the record has none")
    entity: EntityMapping
    emit: list[EmitSpec] = Field(min_length=1)
    attributes: dict[str, str] = Field(default_factory=dict, description="attribute name -> raw field")


class SourcesFile(_M):
    catalog_version: str = "1.0"
    mappings: dict[str, SourceMapping] = Field(default_factory=dict)


CATALOG_FILES: dict[str, type[BaseModel]] = {
    "entities.json": EntitiesFile,
    "signals.json": SignalsFile,
    "domains.json": DomainsFile,
    "correlation.json": CorrelationFile,
    "sources.json": SourcesFile,
    "investigation.json": InvestigationFile,
}


def rule_id(signal_name: str, rule: Any) -> str:
    return rule.id or f"{signal_name}.{rule.type}"


Condition.model_rebuild()
When.model_rebuild()
