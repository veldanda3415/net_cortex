"""The closed set of detection rule types, as pure functions.

    threshold        gt / lt / between on the value or an attribute
    state_equals     value or attribute equals a configured state
    zscore_baseline  |z| > threshold vs baseline_window samples, else the
                     baseline provider, else an optional threshold fallback
    rate_change      value vs the mean of earlier samples of the same series
    event_presence   name in an allow-list and severity >= a minimum

No expression evaluation: a rule is a typed record from the catalog, and
each type has exactly one interpretation here.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from contract import Signal
from contract.catalog_models import (
    SEVERITIES,
    EventPresenceRule,
    RateChangeRule,
    StateEqualsRule,
    ThresholdRule,
    ZScoreBaselineRule,
)
from models.schemas import EntityBaseline

SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

SeriesKey = tuple[str, str, str]  # (signal name, entity type, entity id)


def series_key(signal: Signal) -> SeriesKey:
    return (signal.name, signal.entity.type, signal.entity.id)


BaselineLookup = Callable[[str, str], EntityBaseline | None]


@dataclass
class RuleContext:
    region: str
    # Earlier samples of each series inside the query window, oldest first.
    series: dict[SeriesKey, list[Signal]] = field(default_factory=dict)
    # Samples of each series inside baseline_window (before the query window).
    baseline_series: dict[SeriesKey, list[Signal]] = field(default_factory=dict)
    baseline_lookup: BaselineLookup | None = None
    legacy_fallback: bool = True


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    reason: str
    z: float | None = None


def field_value(signal: Signal, path: str) -> Any:
    if path == "value":
        return signal.value
    if path == "name":
        return signal.name
    if path == "entity_id":
        return signal.entity.id
    if path == "entity_type":
        return signal.entity.type
    if path.startswith("attributes."):
        return signal.attributes.get(path.split(".", 1)[1])
    if path.startswith("labels."):
        return signal.entity.labels.get(path.split(".", 1)[1])
    raise ValueError(f"unsupported field path '{path}'")


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(x: float) -> str:
    return f"{x:g}"


def threshold(rule: ThresholdRule, signal: Signal, rule_id: str) -> RuleHit | None:
    value = _num(field_value(signal, rule.field))
    if value is None:
        return None
    if rule.op == "gt":
        hit = value > rule.value  # type: ignore[operator]
        default = f"{signal.name} {_fmt(value)} > {_fmt(rule.value)}"  # type: ignore[arg-type]
    elif rule.op == "lt":
        hit = value < rule.value  # type: ignore[operator]
        default = f"{signal.name} {_fmt(value)} < {_fmt(rule.value)}"  # type: ignore[arg-type]
    else:
        hit = rule.min <= value <= rule.max  # type: ignore[operator]
        default = f"{signal.name} {_fmt(value)} in [{_fmt(rule.min)}, {_fmt(rule.max)}]"  # type: ignore[arg-type]
    return RuleHit(rule_id, rule.reason or default) if hit else None


def state_equals(rule: StateEqualsRule, signal: Signal, rule_id: str) -> RuleHit | None:
    actual = field_value(signal, rule.field)
    expected = rule.value
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        a = _num(actual)
        equal = a is not None and a == float(expected)
    else:
        equal = actual == expected
    if not equal:
        return None
    return RuleHit(rule_id, rule.reason or f"{signal.name} == {expected}")


def _baseline_key(template: str, signal: Signal, region: str) -> str | None:
    prefix, _, source = template.partition("=")
    if source == "entity_id":
        value = signal.entity.id
    elif source == "entity_type":
        value = signal.entity.type
    elif source == "region":
        value = str(signal.attributes.get("region") or signal.entity.labels.get("region") or region)
    else:  # label:NAME
        value = signal.entity.labels.get(source.split(":", 1)[1])
    return f"{prefix}:{value}" if value else None


def zscore_baseline(rule: ZScoreBaselineRule, signal: Signal, ctx: RuleContext, rule_id: str) -> RuleHit | None:
    value = _num(signal.value)
    if value is None:
        return None

    samples = [s.value for s in ctx.baseline_series.get(series_key(signal), []) if _num(s.value) is not None]
    if len(samples) >= rule.min_baseline_samples:
        mean = statistics.fmean(samples)
        std = statistics.pstdev(samples)
        if std <= 0:
            return None
        z = abs(value - mean) / std
        return RuleHit(rule_id, rule.reason or f"{signal.name} z={z:.2f}", z) if z > rule.threshold else None

    if ctx.baseline_lookup is not None:
        metric = rule.baseline_metric or signal.name
        for template in rule.baseline_keys:
            key = _baseline_key(template, signal, ctx.region)
            if key is None:
                continue
            baseline = ctx.baseline_lookup(key, metric)
            if baseline is None:
                continue
            # Same semantics as providers.baseline_utils: a zero-variance
            # baseline never flags, and a found baseline blocks the fallback.
            if baseline.std_dev <= 0:
                return None
            z = abs(value - baseline.mean) / baseline.std_dev
            return RuleHit(rule_id, rule.reason or f"{signal.name} z={z:.2f}", z) if z > rule.threshold else None

    if rule.fallback is not None and ctx.legacy_fallback:
        return threshold(rule.fallback, signal, f"{rule_id}.fallback")
    return None


def rate_change(rule: RateChangeRule, signal: Signal, ctx: RuleContext, rule_id: str) -> RuleHit | None:
    value = _num(signal.value)
    if value is None:
        return None
    key = series_key(signal)
    reference_samples = [s.value for s in ctx.baseline_series.get(key, []) if _num(s.value) is not None]
    if not reference_samples:
        reference_samples = [
            s.value for s in ctx.series.get(key, [])
            if s.timestamp < signal.timestamp and _num(s.value) is not None
        ]
    if not reference_samples:
        return None
    reference = statistics.fmean(reference_samples)
    if reference <= rule.min_reference:
        return None
    if rule.direction == "increase":
        hit = value >= reference * rule.factor
    else:
        hit = value <= reference / rule.factor
    if not hit:
        return None
    return RuleHit(rule_id, rule.reason or f"{signal.name} {rule.direction} x{(value / reference if rule.direction == 'increase' else reference / max(value, 1e-9)):.1f} vs recent level")


def event_presence(rule: EventPresenceRule, signal: Signal, allowed_names: list[str], rule_id: str) -> RuleHit | None:
    names = rule.names if rule.names is not None else allowed_names
    if signal.name not in names:
        return None
    if SEVERITY_RANK[signal.severity] < SEVERITY_RANK[rule.min_severity]:
        return None
    return RuleHit(rule_id, rule.reason or f"{signal.name} ({signal.severity})")


def evaluate(rule: Any, signal: Signal, ctx: RuleContext, *, rule_id: str, catalog_names: list[str]) -> RuleHit | None:
    """Dispatch on the closed rule-type set."""
    if rule.type == "threshold":
        return threshold(rule, signal, rule_id)
    if rule.type == "state_equals":
        return state_equals(rule, signal, rule_id)
    if rule.type == "zscore_baseline":
        return zscore_baseline(rule, signal, ctx, rule_id)
    if rule.type == "rate_change":
        return rate_change(rule, signal, ctx, rule_id)
    if rule.type == "event_presence":
        return event_presence(rule, signal, catalog_names, rule_id)
    raise ValueError(f"unknown rule type '{rule.type}'")
