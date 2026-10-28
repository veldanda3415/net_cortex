"""CloudWatch metrics -> contract v2 metric signals.

Config (sources.cloudwatch):

    period_seconds: 60
    max_series: 300              # across all entries; more marks coverage truncated
    metrics:
      - namespace: AWS/VPN
        names: [TunnelState]
        stat: Minimum
        dimensions: [TunnelIpAddress]   # exact dimension set of the series to read
        entity: {type: tunnel, id: TunnelIpAddress}

Each entry is discovered with ListMetrics, keeping only series whose
dimension names are exactly `dimensions`, then read with GetMetricData. The
signal name is the CloudWatch metric name, which is what signals.json keys
on. Every dimension becomes an entity label; `entity.id` names the dimension
used as the entity id.

A metric that publishes nothing (instance stopped, agent offline) produces no
signal: absence is not detectable here.

Reads: cloudwatch:ListMetrics, cloudwatch:GetMetricData.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contract import Coverage, Entity, Signal, TelemetryQuery
from contract.catalog_loader import get_catalog
from mcp_telemetry_server.plugins.aws.base import AwsPlugin, now_utc, utc
from mcp_telemetry_server.plugins.base import register_plugin

BASELINE_LOOKBACK = timedelta(hours=24)  # matches TelemetryService's baseline window
MAX_QUERIES_PER_CALL = 500               # GetMetricData limit


class _Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    id: str = Field(description="Dimension whose value is the entity id")


class _MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str
    names: list[str] = Field(min_length=1)
    stat: str = "Average"
    dimensions: list[str] = Field(min_length=1)
    entity: _Entity
    unit: str | None = None


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    period_seconds: int = Field(default=60, ge=1)
    max_series: int = Field(default=300, ge=1)
    metrics: list[_MetricSpec] = Field(min_length=1)


@register_plugin
class CloudWatchPlugin(AwsPlugin):
    name = "cloudwatch"
    domains = frozenset({"metrics"})

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        try:
            self._cfg = _Config.model_validate(self.config)
        except ValueError as exc:
            raise ValueError(f"ConfigValidationError: sources.cloudwatch: {exc}") from None
        for spec in self._cfg.metrics:
            if spec.entity.id not in spec.dimensions:
                raise ValueError(
                    f"ConfigValidationError: sources.cloudwatch: {spec.namespace} entity.id "
                    f"'{spec.entity.id}' is not one of its dimensions {spec.dimensions}"
                )

    async def _discover(self, region: str) -> list[tuple[_MetricSpec, str, dict[str, str]]]:
        series: list[tuple[_MetricSpec, str, dict[str, str]]] = []
        for spec in self._cfg.metrics:
            for metric_name in spec.names:
                pages, _ = await self.paginate(
                    "cloudwatch", region, "list_metrics", max_pages=20,
                    Namespace=spec.namespace, MetricName=metric_name,
                    Dimensions=[{"Name": d} for d in spec.dimensions],
                )
                for page in pages:
                    for metric in page.get("Metrics", []):
                        dims = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
                        if set(dims) == set(spec.dimensions):
                            series.append((spec, metric_name, dims))
        return series

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        region = self.region_for(query)
        series = await self._discover(region)
        truncated = len(series) > self._cfg.max_series
        series = series[: self._cfg.max_series]
        start = query.start_time - BASELINE_LOOKBACK if query.include_baseline else query.start_time
        period = self._cfg.period_seconds
        observed = now_utc()
        catalog = get_catalog()

        signals: list[Signal] = []
        for offset in range(0, len(series), MAX_QUERIES_PER_CALL):
            batch = series[offset: offset + MAX_QUERIES_PER_CALL]
            queries = [
                {
                    "Id": f"m{offset + i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": spec.namespace, "MetricName": metric_name,
                            "Dimensions": [{"Name": k, "Value": v} for k, v in sorted(dims.items())],
                        },
                        "Period": period,
                        "Stat": spec.stat,
                    },
                    "ReturnData": True,
                }
                for i, (spec, metric_name, dims) in enumerate(batch)
            ]
            pages, more = await self.paginate(
                "cloudwatch", region, "get_metric_data", max_pages=50,
                MetricDataQueries=queries, StartTime=start, EndTime=query.end_time, ScanBy="TimestampAscending",
            )
            truncated = truncated or more
            by_id = {q["Id"]: s for q, s in zip(queries, batch)}
            for page in pages:
                for result in page.get("MetricDataResults", []):
                    spec, metric_name, dims = by_id[result["Id"]]
                    found = catalog.signal(metric_name)
                    unit = spec.unit or (found[1].unit if found else None)
                    dim_key = ",".join(f"{k}={v}" for k, v in sorted(dims.items()))
                    entity = Entity(type=spec.entity.type, id=dims[spec.entity.id], labels=dict(dims))
                    for ts, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                        ts = utc(ts)
                        signals.append(Signal(
                            signal_type="metric", timestamp=ts, observed_at=observed, entity=entity,
                            name=metric_name, value=float(value), unit=unit, domain="metrics", source=self.name,
                            attributes={"namespace": spec.namespace, "statistic": spec.stat, "period": period,
                                        "region": region},
                            evidence_ref=f"cloudwatch:{spec.namespace}/{metric_name}/{dim_key}@{ts.isoformat()}",
                        ))
        signals.sort(key=lambda s: s.timestamp)
        status = "partial" if truncated else "ok"
        error = f"series or pages capped at max_series={self._cfg.max_series}" if truncated else None
        return signals, self.coverage(status, truncated=truncated, error=error)
