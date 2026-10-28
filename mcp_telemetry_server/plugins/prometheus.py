"""Prometheus / Mimir range queries -> contract v2 metric signals.

Config (sources.prometheus):

    url: http://prometheus:9090            # Mimir: https://mimir.example/prometheus
    tenant: team-network                   # optional; sent as X-Scope-OrgID (Mimir, Cortex)
    auth: none                             # none | bearer_env | basic_env
    token_env: PROM_TOKEN                  # bearer_env: the token's env var
    user_env: PROM_USER                    # basic_env
    password_env: PROM_PASSWORD
    step_seconds: 60
    timeout_seconds: 15
    max_series: 500                        # across all queries; more marks coverage truncated
    queries:
      - name: link_out_bps                 # the signal name signals.json judges
        expr: 'rate(ifHCOutOctets{job="snmp"}[5m]) * 8'
        entity: {type: interface, id: ifName, labels: [instance, ifAlias]}
        capacity_expr: 'ifHighSpeed{job="snmp"} * 1e6'   # optional: becomes labels.capacity_bps
        unit: Bits/Second
      - name: error_rate
        expr: '100 * rate(ifInErrors[5m]) / clamp_min(rate(ifHCInUcastPkts[5m]), 1)'
        entity: {type: interface, id: ifName}

Each query runs as /api/v1/query_range over the window (and the 24 h before
it when the query asks for a baseline). Every series becomes an entity: the
`entity.id` label is its id, and the listed labels are kept. With
`capacity_expr`, each series gets labels.capacity_bps from the matching
capacity series (same entity id), so a threshold `per: labels.capacity_bps`
rule turns traffic into utilization. Absence of a series is not a signal.

Credentials come from the environment, never from the config file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from contract import Coverage, Entity, Signal, TelemetryQuery
from mcp_telemetry_server.plugins.base import BASELINE_LOOKBACK, SourcePlugin, register_plugin


class _Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    id: str = Field(description="Series label whose value is the entity id")
    labels: list[str] = Field(default_factory=list, description="Series labels kept as entity labels")


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    expr: str
    entity: _Entity
    capacity_expr: str | None = None
    unit: str | None = None


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str
    tenant: str | None = None
    auth: str = Field(default="none", pattern="^(none|bearer_env|basic_env)$")
    token_env: str | None = None
    user_env: str | None = None
    password_env: str | None = None
    step_seconds: int = Field(default=60, ge=1)
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_series: int = Field(default=500, ge=1)
    queries: list[_Query] = Field(min_length=1)


@register_plugin
class PrometheusPlugin(SourcePlugin):
    name = "prometheus"
    domains = frozenset({"metrics"})
    needs_cloud_credentials = False

    def __init__(self, config: dict[str, Any] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(config)
        try:
            self._cfg = _Config.model_validate(self.config)
        except ValueError as exc:
            raise ValueError(f"ConfigValidationError: sources.prometheus: {exc}") from None
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers: dict[str, str] = {}
        auth = None
        if self._cfg.tenant:
            headers["X-Scope-OrgID"] = self._cfg.tenant
        if self._cfg.auth == "bearer_env":
            headers["Authorization"] = f"Bearer {os.environ.get(self._cfg.token_env or '', '')}"
        elif self._cfg.auth == "basic_env":
            auth = (os.environ.get(self._cfg.user_env or "", ""), os.environ.get(self._cfg.password_env or "", ""))
        return httpx.AsyncClient(base_url=self._cfg.url.rstrip("/"), headers=headers, auth=auth,
                                 timeout=self._cfg.timeout_seconds, transport=self._transport)

    async def _range(self, client: httpx.AsyncClient, expr: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        response = await client.get("/api/v1/query_range", params={
            "query": expr, "start": start.timestamp(), "end": end.timestamp(), "step": self._cfg.step_seconds,
        })
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {body.get('errorType')}: {body.get('error')}")
        return body.get("data", {}).get("result", [])

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        start = query.start_time - BASELINE_LOOKBACK if query.include_baseline else query.start_time
        observed = datetime.now(timezone.utc)
        signals: list[Signal] = []
        series_seen = 0
        truncated = False
        async with self._client() as client:
            for spec in self._cfg.queries:
                capacity: dict[str, str] = {}
                if spec.capacity_expr:
                    for series in await self._range(client, spec.capacity_expr, query.end_time, query.end_time):
                        eid = series.get("metric", {}).get(spec.entity.id)
                        if eid is not None and series.get("values"):
                            capacity[eid] = series["values"][-1][1]
                for series in await self._range(client, spec.expr, start, query.end_time):
                    labels = series.get("metric", {})
                    eid = labels.get(spec.entity.id)
                    if eid is None:
                        continue
                    if series_seen >= self._cfg.max_series:
                        truncated = True
                        break
                    series_seen += 1
                    kept = {k: str(labels[k]) for k in spec.entity.labels if k in labels}
                    if eid in capacity:
                        kept["capacity_bps"] = str(capacity[eid])
                    entity = Entity(type=spec.entity.type, id=str(eid), labels=kept)
                    selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
                    for ts, raw in series.get("values", []):
                        try:
                            value = float(raw)
                        except (TypeError, ValueError):
                            continue
                        when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                        signal = Signal(
                            signal_type="metric", timestamp=when, observed_at=observed, entity=entity, name=spec.name,
                            value=value, unit=spec.unit, domain="metrics", source=self.name,
                            attributes={"expr": spec.expr, "region": query.region},
                            evidence_ref=f"prometheus:{spec.name}{{{selector}}}@{when.isoformat()}",
                        )
                        if query.matches(signal):
                            signals.append(signal)
        status = "partial" if truncated else "ok"
        return signals, self.coverage(status, truncated=truncated,
                                      error=f"series capped at max_series={self._cfg.max_series}" if truncated else None)

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "url": self._cfg.url, "queries": len(self._cfg.queries)}
