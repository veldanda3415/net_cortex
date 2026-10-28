"""CloudWatch Logs -> named log events (or per-minute counts).

Rules only match a signal's name, never its text (CONTRACT.md), so this
plugin names each error class at the source. Every configured query reads
one log group with FilterLogEvents, parses each line, and keeps only the lines
matched by a `classify` rule. The rule's name becomes the signal name:

    deny_log_groups: [/netcortex-lab/faults]    # refused at startup if queried
    max_events: 2000                             # per query; more marks coverage truncated
    queries:
      - id: frr
        log_group: /netcortex-lab/router/frr
        domain: logs                             # or routing
        filter_pattern: '?ADJCHANGE ?NOTIFICATION'
        format: text                             # text | json | fields (space-delimited, see `fields`)
        entity: {type: bgp_neighbor}             # id defaults to the log stream name
        classify:
          - name: bgp_neighbor_down
            pattern: 'ADJCHANGE: neighbor (?P<id>[0-9.]+).* Down'   # (?P<id>) sets the entity id
            severity: error
        aggregate: none                          # or count_per_minute: one metric per name/entity/minute
        optional: false                          # true: a missing log group is skipped silently

A classify rule tests `field` (a dotted path into the parsed record; default
`@message`, the raw line; `@stream` is the log stream) with `equals` and/or
`pattern`. The first matching rule wins. A rule can override the query's
`entity`, whose `field` and `pattern` work the same way. These patterns are
source-side parsing, not detection: whether a named event is anomalous is
still decided by its rule in signals.json.

Reads: logs:FilterLogEvents.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contract import Coverage, Entity, Signal, TelemetryQuery
from contract.catalog_models import Severity
from mcp_telemetry_server.plugins.aws.base import AwsPlugin, now_utc
from mcp_telemetry_server.plugins.base import register_plugin

MESSAGE = "@message"
STREAM = "@stream"
MAX_MESSAGE_CHARS = 500
MAX_CONCURRENT_QUERIES = 4


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _EntitySpec(_M):
    type: str
    field: str = STREAM
    pattern: str | None = Field(default=None, description="Regex; its (?P<id>...) group is the id")


class _Classify(_M):
    name: str
    severity: Severity = "error"
    field: str = MESSAGE
    equals: str | None = None
    pattern: str | None = None
    entity: _EntitySpec | None = None

    @model_validator(mode="after")
    def _has_test(self) -> "_Classify":
        if self.equals is None and self.pattern is None:
            raise ValueError(f"classify '{self.name}' needs `equals` or `pattern`")
        return self


class _Query(_M):
    id: str
    log_group: str
    domain: str = "logs"
    optional: bool = Field(default=False, description="A missing log group is skipped, not an error")
    filter_pattern: str = ""
    format: Literal["text", "json", "fields"] = "text"
    fields: list[str] = Field(default_factory=list)
    timestamp_field: str | None = None
    entity: _EntitySpec
    classify: list[_Classify] = Field(min_length=1)
    aggregate: Literal["none", "count_per_minute"] = "none"
    attributes: dict[str, str] = Field(default_factory=dict, description="attribute name -> record field")

    @model_validator(mode="after")
    def _fields_format(self) -> "_Query":
        if self.format == "fields" and not self.fields:
            raise ValueError(f"query '{self.id}' has format: fields but no `fields`")
        return self


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    deny_log_groups: list[str] = Field(default_factory=list)
    max_events: int = Field(default=2000, ge=1)
    queries: list[_Query] = Field(min_length=1)


def _get(record: dict[str, Any], path: str) -> Any:
    if path in record:
        return record[path]
    value: Any = record
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _parse(query: _Query, event: dict[str, Any]) -> dict[str, Any] | None:
    message = event.get("message", "")
    record: dict[str, Any] = {MESSAGE: message, STREAM: event.get("logStreamName", "")}
    if query.format == "json":
        try:
            parsed = json.loads(message)
        except ValueError:
            return None
        if isinstance(parsed, dict):
            record.update(parsed)
    elif query.format == "fields":
        record.update(zip(query.fields, message.split()))
    return record


def _timestamp(query: _Query, record: dict[str, Any], event: dict[str, Any]) -> datetime:
    raw = _get(record, query.timestamp_field) if query.timestamp_field else None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        seconds = float(raw)
        return datetime.fromtimestamp(seconds / 1000 if seconds > 1e12 else seconds, tz=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(event["timestamp"] / 1000, tz=timezone.utc)


def _entity_id(spec: _EntitySpec, record: dict[str, Any], match: re.Match | None) -> str:
    if match is not None and "id" in match.groupdict() and match.group("id"):
        return match.group("id")
    value = _get(record, spec.field)
    value = "" if value is None else str(value)
    if spec.pattern:
        m = re.search(spec.pattern, value)
        if m:
            return m.groupdict().get("id") or m.group(0)
    return value


def _denied(group: str, deny: list[str]) -> bool:
    return any(group == d or (d.endswith("*") and group.startswith(d[:-1])) for d in deny)


@register_plugin
class CloudWatchLogsPlugin(AwsPlugin):
    name = "cloudwatch_logs"
    domains = frozenset({"logs", "routing"})

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        try:
            self._cfg = _Config.model_validate(self.config)
        except ValueError as exc:
            raise ValueError(f"ConfigValidationError: sources.cloudwatch_logs: {exc}") from None
        for q in self._cfg.queries:
            if _denied(q.log_group, self._cfg.deny_log_groups):
                raise ValueError(
                    f"ConfigValidationError: sources.cloudwatch_logs query '{q.id}' reads {q.log_group}, "
                    "which is in deny_log_groups"
                )
            if q.domain not in self.domains:
                raise ValueError(
                    f"ConfigValidationError: sources.cloudwatch_logs query '{q.id}' has domain '{q.domain}', "
                    f"but this plugin serves {sorted(self.domains)}"
                )
        self._patterns = {
            (q.id, i): re.compile(c.pattern) for q in self._cfg.queries for i, c in enumerate(q.classify) if c.pattern
        }

    def _classify(self, query: _Query, record: dict[str, Any]) -> tuple[_Classify, re.Match | None] | None:
        for i, rule in enumerate(query.classify):
            value = _get(record, rule.field)
            value = "" if value is None else str(value)
            if rule.equals is not None and value != rule.equals:
                continue
            match = None
            if rule.pattern is not None:
                match = self._patterns[(query.id, i)].search(value)
                if match is None:
                    continue
            return rule, match
        return None

    async def _run(self, query: _Query, q: TelemetryQuery, region: str) -> tuple[list[Signal], bool]:
        params: dict[str, Any] = {
            "logGroupName": query.log_group,
            "startTime": int(q.start_time.timestamp() * 1000),
            "endTime": int(q.end_time.timestamp() * 1000),
        }
        if query.filter_pattern:
            params["filterPattern"] = query.filter_pattern
        pages, more = await self.paginate("logs", region, "filter_log_events", max_pages=100, **params)
        events = [e for page in pages for e in page.get("events", [])]
        truncated = more or len(events) > self._cfg.max_events
        events = events[: self._cfg.max_events]
        observed = now_utc()

        signals: list[Signal] = []
        counts: dict[tuple[str, str, str, datetime], tuple[Signal, int]] = {}
        for event in events:
            record = _parse(query, event)
            if record is None:
                continue
            found = self._classify(query, record)
            if found is None:
                continue
            rule, match = found
            spec = rule.entity or query.entity
            entity = Entity(type=spec.type, id=_entity_id(spec, record, match))
            ts = _timestamp(query, record, event)
            attributes = {k: _get(record, f) for k, f in query.attributes.items() if _get(record, f) is not None}
            attributes.update(log_group=query.log_group, message=str(record[MESSAGE])[:MAX_MESSAGE_CHARS], region=region)
            if query.aggregate == "count_per_minute":
                minute = ts.replace(second=0, microsecond=0)
                key = (rule.name, entity.type, entity.id, minute)
                first, n = counts.get(key, (None, 0))
                if first is None:
                    attributes["sample"] = attributes.pop("message")
                    first = Signal(
                        signal_type="metric", timestamp=minute, observed_at=observed, entity=entity,
                        name=rule.name, unit="Count", severity=rule.severity, domain=query.domain, source=self.name,
                        attributes=attributes,
                        evidence_ref=f"cloudwatch_logs:{query.log_group}/{rule.name}/{entity.id}@{minute.isoformat()}",
                    )
                counts[key] = (first, n + 1)
                continue
            signals.append(Signal(
                signal_type="event", timestamp=ts, observed_at=observed, entity=entity, name=rule.name,
                severity=rule.severity, domain=query.domain, source=self.name, attributes=attributes,
                evidence_ref=f"cloudwatch_logs:{query.log_group}/{event.get('logStreamName', '')}/{event.get('eventId', '')}",
            ))
        for first, n in counts.values():
            signals.append(first.model_copy(update={"value": float(n)}))
        return signals, truncated

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        region = self.region_for(query)
        queries = [q for q in self._cfg.queries if q.domain == domain]
        # Concurrent, but few at a time: FilterLogEvents has a low per-account TPS quota.
        gate = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)

        async def run(q: _Query) -> tuple[list[Signal], bool]:
            async with gate:
                return await self._run(q, query, region)

        results = await asyncio.gather(*(run(q) for q in queries), return_exceptions=True)
        signals: list[Signal] = []
        errors: list[str] = []
        truncated = False
        for q, result in zip(queries, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):  # one unreadable log group must not hide the others
                if q.optional and type(result).__name__ == "ResourceNotFoundException":
                    continue
                errors.append(f"{q.id}: {type(result).__name__}: {result}")
                continue
            got, cut = result
            signals.extend(got)
            truncated = truncated or cut
        if queries and len(errors) == len(queries):
            raise RuntimeError("; ".join(errors))
        signals.sort(key=lambda s: s.timestamp)
        problems = errors + ([f"event cap max_events={self._cfg.max_events} hit"] if truncated else [])
        status = "partial" if problems else "ok"
        return signals, self.coverage(status, truncated=truncated, error="; ".join(problems) or None)
