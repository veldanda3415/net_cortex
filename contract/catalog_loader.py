"""Load, merge and validate the detection catalog.

Resolution: the default catalog shipped in contract/catalog/, merged with
an optional override directory ($NETCORTEX_CATALOG_DIR). Each override file
replaces default entries *by key* (a whole signal / domain / rule entry at
a time; top-level scalars like catalog_version are replaced too).

Validation runs in three passes and the first failure names the file and
field: JSON Schema per file, typed models, then cross-file references.
A process that loads an invalid catalog refuses to start.

    python -m contract.catalog_loader validate <dir>   # same as `app/main.py catalog validate`
    python -m contract.catalog_loader export-schema [--check]
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel, ValidationError

from contract.catalog_models import (
    CATALOG_FILES,
    KNOWN_TOOLS,
    Condition,
    CorrelationFile,
    CorrelationRule,
    DomainDef,
    DomainsFile,
    EntitiesFile,
    InvestigationFile,
    SignalDef,
    SignalsFile,
    SourceMapping,
    SourcesFile,
)

DEFAULT_CATALOG_DIR = Path(__file__).resolve().parent / "catalog"
SCHEMA_DIR = DEFAULT_CATALOG_DIR / "schema"
CATALOG_ENV = "NETCORTEX_CATALOG_DIR"

# Placeholders summary templates may use, by root name. Anything else is a
# validation error, so a typo can't silently render as "unknown" in prod.
SUMMARY_ROOTS = {
    "hit_count", "candidate_count", "observation_count", "entity_count", "top_entity",
    "first", "latest", "worst", "derived", "domain",
}
NOTE_ROOTS = {"peer_anomaly_domains"}


class CatalogError(ValueError):
    """Invalid catalog. Message starts with '<file>: <field path>: '."""


def _schema_for(filename: str) -> dict[str, Any]:
    return CATALOG_FILES[filename].model_json_schema()


def schema_file_name(filename: str) -> str:
    return filename.replace(".json", ".schema.json")


def render_schemas() -> dict[str, str]:
    out = {}
    for filename in CATALOG_FILES:
        schema = _schema_for(filename)
        schema["$id"] = f"https://netcortex.dev/contract/catalog/{schema_file_name(filename)}"
        out[schema_file_name(filename)] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out


def _path(parts) -> str:
    return ".".join(str(p) for p in parts) or "<root>"


def _validate_file(filename: str, data: Any, label: str) -> None:
    validator = jsonschema.Draft202012Validator(_schema_for(filename))
    errors = sorted(validator.iter_errors(data), key=lambda e: (len(list(e.absolute_path)), str(e.message)))
    if errors:
        best = jsonschema.exceptions.best_match(errors)
        raise CatalogError(f"{label}: {_path(best.absolute_path)}: {best.message}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path}: line {exc.lineno} col {exc.colno}: invalid JSON: {exc.msg}") from None


def _merge(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(default)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}  # entry-level: override entry wins whole
        else:
            merged[key] = value
    return merged


@dataclass
class Catalog:
    entities: EntitiesFile
    signals: SignalsFile
    domains: DomainsFile
    correlation: CorrelationFile
    sources: SourcesFile
    investigation: InvestigationFile = field(default_factory=InvestigationFile)
    origin: list[str] = field(default_factory=list)
    _by_name: dict[str, tuple[str, SignalDef]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for name, sig in self.signals.signals.items():
            for alias in [name, *sig.match_names]:
                self._by_name[alias] = (name, sig)

    # -- lookups ---------------------------------------------------------------
    def signal(self, raw_name: str) -> tuple[str, SignalDef] | None:
        """(catalog name, definition) for a raw signal name, or None if unknown."""
        return self._by_name.get(raw_name)

    def domain(self, domain_id: str) -> DomainDef:
        return self.domains.domains[domain_id]

    @property
    def domain_ids(self) -> list[str]:
        return list(self.domains.domains)

    def telemetry_domains(self) -> dict[str, str]:
        """telemetry domain -> MCP tool."""
        return {d.telemetry_domain: d.tool for d in self.domains.domains.values()}

    def domain_for_telemetry(self, telemetry_domain: str) -> str | None:
        for domain_id, d in self.domains.domains.items():
            if d.telemetry_domain == telemetry_domain:
                return domain_id
        return None

    def active_domains(self, degradation_type: str) -> list[str]:
        dmap = self.domains.degradation_map
        return list(dmap.get(degradation_type, dmap["unknown"]))

    def correlation_rules(self, domain_id: str) -> list[tuple[str, CorrelationRule]]:
        rules = [(rid, r) for rid, r in self.correlation.rules.items() if r.domain == domain_id]
        return sorted(rules, key=lambda item: (item[1].priority, item[0]))

    def mappings_for(self, source: str) -> dict[str, SourceMapping]:
        return {mid: m for mid, m in self.sources.mappings.items() if m.source == source}

    def signal_role(self, raw_name: str) -> str | None:
        """'cause', 'symptom' or None, by raw name first, then catalog name."""
        roles = self.investigation.signal_roles
        if raw_name in roles:
            return roles[raw_name]
        found = self.signal(raw_name)
        return roles.get(found[0]) if found else None


def _template_roots(template: str) -> set[str]:
    roots = set()
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None:
            roots.add(field_name.split(".", 1)[0].split("[", 1)[0])
    return roots


def _check_condition(cond: Condition, where: str, domains: set[str]) -> None:
    set_keys = [k for k in ("key_event", "summary_contains", "peer_anomaly", "any", "all") if getattr(cond, k) is not None]
    if len(set_keys) != 1:
        raise CatalogError(f"{where}: a condition must set exactly one of key_event/summary_contains/peer_anomaly/any/all, got {set_keys}")
    for key in ("key_event", "summary_contains", "peer_anomaly"):
        part = getattr(cond, key)
        if part is not None and part.domains:
            unknown = set(part.domains) - domains
            if unknown:
                raise CatalogError(f"{where}.{key}.domains: unknown domain(s) {sorted(unknown)}")
    for key in ("any", "all"):
        for i, sub in enumerate(getattr(cond, key) or []):
            _check_condition(sub, f"{where}.{key}.{i}", domains)


def cross_validate(cat: Catalog) -> None:
    entity_types = set(cat.entities.entity_types)
    domains = set(cat.domains.domains)
    signals = cat.signals.signals

    for rid, rel in cat.entities.relationships.items():
        for side in ("source", "target"):
            unknown = set(getattr(rel, side)) - entity_types
            if unknown:
                raise CatalogError(f"entities.json: relationships.{rid}.{side}: unknown entity type(s) {sorted(unknown)}")

    seen: dict[str, str] = {}
    for name, sig in signals.items():
        where = f"signals.json: signals.{name}"
        if sig.domain not in domains:
            raise CatalogError(f"{where}.domain: unknown domain '{sig.domain}' (domains.json has {sorted(domains)})")
        unknown = set(sig.entity_types) - entity_types
        if unknown:
            raise CatalogError(f"{where}.entity_types: unknown entity type(s) {sorted(unknown)}")
        for alias in [name, *sig.match_names]:
            if alias in seen and seen[alias] != name:
                raise CatalogError(f"{where}.match_names: '{alias}' already claimed by signals.{seen[alias]}")
            seen[alias] = name
        rule = sig.rule
        if rule is not None and rule.type == "threshold":
            if rule.per is not None and not rule.per.startswith(("attributes.", "labels.")):
                raise CatalogError(f"{where}.rule.per: '{rule.per}' must be 'attributes.<name>' or 'labels.<name>'")
            if rule.op == "between" and (rule.min is None or rule.max is None):
                raise CatalogError(f"{where}.rule: op 'between' needs min and max")
            if rule.op != "between" and rule.value is None:
                raise CatalogError(f"{where}.rule.value: required for op '{rule.op}'")
        if rule is not None and rule.type == "zscore_baseline":
            for i, key in enumerate(rule.baseline_keys):
                prefix, _, src = key.partition("=")
                if not prefix or not (src in ("entity_id", "entity_type", "region") or src.startswith("label:")):
                    raise CatalogError(f"{where}.rule.baseline_keys.{i}: '{key}' must be '<prefix>=<entity_id|entity_type|region|label:NAME>'")

    ports: dict[int, str] = {}
    skills: dict[str, str] = {}
    telemetry: dict[str, str] = {}
    for did, d in cat.domains.domains.items():
        where = f"domains.json: domains.{did}"
        if d.tool not in KNOWN_TOOLS:
            raise CatalogError(f"{where}.tool: unknown MCP tool '{d.tool}'; known: {sorted(KNOWN_TOOLS)} (a new tool needs code)")
        fixed = KNOWN_TOOLS[d.tool]
        if fixed is not None and fixed != d.telemetry_domain:
            raise CatalogError(f"{where}.telemetry_domain: tool '{d.tool}' serves '{fixed}', not '{d.telemetry_domain}'")
        if d.telemetry_domain in telemetry:
            raise CatalogError(f"{where}.telemetry_domain: '{d.telemetry_domain}' already used by domains.{telemetry[d.telemetry_domain]}")
        telemetry[d.telemetry_domain] = did
        if d.agent.port in ports:
            raise CatalogError(f"{where}.agent.port: {d.agent.port} already used by domains.{ports[d.agent.port]}")
        ports[d.agent.port] = did
        if d.agent.skill.id in skills or d.agent.skill.id == "respond-to-peer":
            raise CatalogError(f"{where}.agent.skill.id: '{d.agent.skill.id}' is not unique")
        skills[d.agent.skill.id] = did
        for spec_name in ("anomaly", "anomaly_suffixes", "clean"):
            for i, variant in enumerate(getattr(d.summary, spec_name)):
                bad = _template_roots(variant.template) - SUMMARY_ROOTS
                if bad:
                    raise CatalogError(f"{where}.summary.{spec_name}.{i}.template: unknown placeholder(s) {sorted(bad)}; allowed {sorted(SUMMARY_ROOTS)}")
        if d.relevance:
            unknown = set(d.relevance.applies_to) - set(seen)
            if unknown:
                raise CatalogError(f"{where}.relevance.applies_to: unknown signal(s) {sorted(unknown)}")
        for i, der in enumerate(d.derived):
            sig = signals.get(der.signal)
            if sig is None or not sig.derived or sig.domain != did:
                raise CatalogError(f"{where}.derived.{i}.signal: '{der.signal}' must be a derived signal of domain '{did}' in signals.json")

    dmap = cat.domains.degradation_map
    if "unknown" not in dmap:
        raise CatalogError("domains.json: degradation_map.unknown: required (used when classification is uncertain)")
    for dtype, members in dmap.items():
        unknown = set(members) - domains
        if unknown:
            raise CatalogError(f"domains.json: degradation_map.{dtype}: unknown domain(s) {sorted(unknown)}")
        if not members:
            raise CatalogError(f"domains.json: degradation_map.{dtype}: must list at least one domain")

    markers = {"corroboration": "Peer corroboration:", "contradiction": "Peer contradiction:"}
    for rid, rule in cat.correlation.rules.items():
        where = f"correlation.json: rules.{rid}"
        if rule.domain not in domains:
            raise CatalogError(f"{where}.domain: unknown domain '{rule.domain}'")
        if not rule.effect.note.startswith(markers[rule.effect.kind]):
            raise CatalogError(f"{where}.effect.note: must start with '{markers[rule.effect.kind]}' (idempotency marker)")
        bad = _template_roots(rule.effect.note) - NOTE_ROOTS
        if bad:
            raise CatalogError(f"{where}.effect.note: unknown placeholder(s) {sorted(bad)}")
        for i, cond in enumerate(rule.when.all):
            _check_condition(cond, f"{where}.when.all.{i}", domains)

    for etype, probe_domains in cat.investigation.probes.items():
        where = f"investigation.json: probes.{etype}"
        if etype not in entity_types:
            raise CatalogError(f"{where}: unknown entity type '{etype}'")
        unknown = set(probe_domains) - domains
        if unknown:
            raise CatalogError(f"{where}: unknown domain(s) {sorted(unknown)}")

    for pattern, etype in cat.investigation.lead_patterns.items():
        where = f"investigation.json: lead_patterns.{pattern}"
        if etype not in entity_types:
            raise CatalogError(f"{where}: unknown entity type '{etype}'")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CatalogError(f"{where}: invalid regex: {exc}") from None

    for mid, m in cat.sources.mappings.items():
        where = f"sources.json: mappings.{mid}"
        if m.domain not in telemetry:
            raise CatalogError(f"{where}.domain: no domain in domains.json has telemetry_domain '{m.domain}'")
        for i, emit in enumerate(m.emit):
            hit = cat.signal(emit.name)
            if hit is None:
                raise CatalogError(f"{where}.emit.{i}.name: '{emit.name}' is not a catalog signal")
            if cat.domain(hit[1].domain).telemetry_domain != m.domain:
                raise CatalogError(f"{where}.emit.{i}.name: signal '{emit.name}' belongs to domain '{hit[1].domain}', not '{m.domain}'")


def load_catalog(override_dir: str | os.PathLike | None = None, default_dir: str | os.PathLike | None = None) -> Catalog:
    default_dir = Path(default_dir or DEFAULT_CATALOG_DIR)
    override = Path(override_dir) if override_dir else None
    if override is not None and not override.is_dir():
        raise CatalogError(f"{override}: catalog override directory does not exist")
    if override is not None:
        unknown = sorted(p.name for p in override.glob("*.json") if p.name not in CATALOG_FILES)
        if unknown:
            raise CatalogError(f"{override}: unknown catalog file(s) {unknown}; expected a subset of {sorted(CATALOG_FILES)}")

    parsed: dict[str, BaseModel] = {}
    origin: list[str] = []
    for filename, model in CATALOG_FILES.items():
        default_path = default_dir / filename
        data = _read_json(default_path) if default_path.exists() else {}
        _validate_file(filename, data, str(default_path))
        origin.append(str(default_path))
        if override is not None and (override / filename).exists():
            over_path = override / filename
            over = _read_json(over_path)
            _validate_file(filename, over, str(over_path))
            data = _merge(data, over)
            origin.append(str(over_path))
            label = f"{filename} (merged with {over_path})"
        else:
            label = str(default_path)
        _validate_file(filename, data, label)
        try:
            parsed[filename] = model.model_validate(data)
        except ValidationError as exc:
            err = exc.errors()[0]
            raise CatalogError(f"{label}: {_path(err['loc'])}: {err['msg']}") from None

    cat = Catalog(
        entities=parsed["entities.json"],  # type: ignore[arg-type]
        signals=parsed["signals.json"],  # type: ignore[arg-type]
        domains=parsed["domains.json"],  # type: ignore[arg-type]
        correlation=parsed["correlation.json"],  # type: ignore[arg-type]
        sources=parsed["sources.json"],  # type: ignore[arg-type]
        investigation=parsed["investigation.json"],  # type: ignore[arg-type]
        origin=origin,
    )
    cross_validate(cat)
    return cat


@lru_cache(maxsize=1)
def _cached(override: str | None) -> Catalog:
    return load_catalog(override)


def get_catalog() -> Catalog:
    """Process-wide catalog: defaults + $NETCORTEX_CATALOG_DIR (if set)."""
    return _cached(os.environ.get(CATALOG_ENV) or None)


def reset_catalog_cache() -> None:
    _cached.cache_clear()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m contract.catalog_loader")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="Validate defaults merged with an override dir (or the default dir itself)")
    v.add_argument("dir", nargs="?", default=None)
    e = sub.add_parser("export-schema", help="Write contract/catalog/schema/*.schema.json")
    e.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "validate":
        try:
            cat = load_catalog(args.dir)
        except CatalogError as exc:
            print(f"INVALID: {exc}")
            return 1
        print(f"OK: {len(cat.signals.signals)} signals, {len(cat.domains.domains)} domains, "
              f"{len(cat.correlation.rules)} correlation rules, {len(cat.sources.mappings)} source mappings")
        return 0

    stale = []
    SCHEMA_DIR.mkdir(exist_ok=True)
    for name, content in render_schemas().items():
        path = SCHEMA_DIR / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        print(f"Stale catalog schema(s): {stale} — run `python -m contract.catalog_loader export-schema`")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
