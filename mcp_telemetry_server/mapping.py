"""Apply sources.json mappings: raw source record (dict) -> contract v2 Signals.

A mapping only names fields to copy — entity type/id/labels, one signal per
`emit` entry (value, unit, severity), and attributes. No expressions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from contract import Entity, Signal
from contract.catalog_models import SourceMapping


def map_entity(mapping: SourceMapping, record: dict[str, Any]) -> Entity:
    spec = mapping.entity
    labels: dict[str, str] = {}
    if spec.label_field:
        raw = record.get(spec.label_field) or {}
        labels = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    if spec.type and spec.id_field:
        return Entity(type=spec.type, id=str(record.get(spec.id_field, "")), labels=labels)
    for label in spec.type_priority:
        if label in labels:
            return Entity(type=label, id=labels[label], labels=labels)
    if labels:
        # No preferred label: the alphabetically first one identifies the entity.
        first = sorted(labels)[0]
        return Entity(type=first, id=labels[first], labels=labels)
    fallback_id = record.get(spec.fallback_id_field or "", "") if spec.fallback_id_field else ""
    return Entity(type=spec.fallback_type or spec.type or "unknown", id=str(fallback_id), labels=labels)


def map_record(
    mapping: SourceMapping,
    record: dict[str, Any],
    *,
    timestamp: datetime,
    evidence_ref: str,
    observed_at: datetime | None = None,
) -> list[Signal]:
    entity = map_entity(mapping, record)
    attributes = {attr: record[src] for attr, src in mapping.attributes.items() if src in record}
    signals: list[Signal] = []
    for emit in mapping.emit:
        value = None
        if emit.value_field is not None:
            raw = record.get(emit.value_field)
            value = float(raw) if raw is not None else None
        if emit.severity_field is not None:
            severity = emit.severity_map.get(str(record.get(emit.severity_field)), emit.severity or "info")
        else:
            severity = emit.severity or "info"
        ref = f"{evidence_ref}/{emit.name}" if len(mapping.emit) > 1 else evidence_ref
        signals.append(Signal(
            signal_type=mapping.signal_type,
            timestamp=timestamp,
            observed_at=observed_at or timestamp,
            entity=entity,
            name=emit.name,
            value=value,
            unit=emit.unit,
            severity=severity,
            domain=mapping.domain,
            source=mapping.source,
            attributes=dict(attributes),
            evidence_ref=ref,
        ))
    return signals
