# Provider Adapter Guide

This guide explains how to connect real telemetry backends to NetCortex without changing orchestration logic.

## Adapter Principle

NetCortex separates decision logic from data retrieval.

- Adapters fetch raw data from external systems.
- Agents consume normalized provider outputs.
- Orchestrator remains unchanged.

## Current Extension Points

Implement these files:

- `providers/adapters/prometheus_adapter.py`
- `providers/adapters/elk_adapter.py`
- `providers/adapters/splunk_adapter.py`
- `providers/adapters/mcp_adapter.py`
- `providers/adapters/prometheus_baseline_adapter.py` — baseline adapter (stub already present, implement `get_baseline`)

Each adapter should conform to the abstract provider interface for its domain.

## Adapter Design Requirements

1. Deterministic return shape
- Return only schema-compatible objects.
- Convert backend-specific fields to canonical model fields.

2. Time-window fidelity
- Respect requested region and window exactly.
- Avoid hidden default windows.

3. Reliability controls
- Request timeout
- Retry with bounded attempts
- Circuit-breaker or fast-fail mode when upstream is down

4. Data quality metadata
- Return enough context for agent confidence down-weighting when data is stale/incomplete.
- Set the instance-level `degraded: bool` flag (see `providers/base.py`) whenever a fetch/parse failure caused the method to fall back to an empty result — this is separate from the "never raise" contract below and is what lets a caller distinguish "verified nothing wrong" from "telemetry unavailable." See "Reporting Degraded Fetches" further down.

5. Security
- Never log secrets or bearer tokens.
- Use environment variables or secret managers for credentials.

6. Fail on genuinely unresolvable input, don't silently degrade it
- An unknown `scenario_id` (simulation mode) or any input that has no sane fallback should raise a clear, catchable error (e.g. `ValueError(f"Unknown scenario_id: {scenario_id}")`), not return an empty result or crash later with an unrelated `AttributeError`. This is distinct from #4/the "never raise" contract, which is about *transport* failures (timeout, unreachable server) where an empty result is the correct degrade-gracefully behavior. A malformed *request* should fail loudly and immediately.

## Minimal Adapter Contract by Domain

### Metrics Adapter
Must provide:
- timestamp
- region
- error_rate
- packet_loss
- throughput_gbps
- latency_ms

Example normalized row:

```json
{
	"timestamp": "2026-05-05T10:14:00Z",
	"region": "us-east",
	"error_rate": 0.081,
	"packet_loss": 0.024,
	"throughput_gbps": 0.61,
	"latency_ms": 124.5,
	"tags": {
		"service": "gateway",
		"node": "edge-3"
	}
}
```

### Log Adapter
Must provide:
- timestamp
- level
- service
- message

### Routing Adapter
Must provide:
- timestamp
- region
- path_id
- change_type
- details

### Config Adapter
Must provide:
- timestamp
- component
- change_type
- before
- after

## Backend Mapping Notes

### Prometheus
- Map queries to normalized metric snapshots.
- Use recording rules when raw queries are too expensive.

### ELK/Splunk
- Normalize severity and service fields.
- Keep message truncation deterministic.

### MCP
- Keep MCP tool contracts stable.
- Validate response payload shape before model conversion.

### Simulation

`providers/simulation/*.py` are the default, network-free providers backing `SCENARIOS`. Two things worth knowing if you're extending them:

- `SCENARIOS.get(scenario_id or 1)` returning `None` for an unknown id now raises `ValueError(f"Unknown scenario_id: {scenario_id}")` in every simulation provider, rather than letting the `None` propagate into an unrelated `AttributeError` on first attribute access.
- `LogEvent` and `ConfigChange` carry no per-row `region` field (unlike `MetricSnapshot`/`RoutingEvent`), so `SimulationLogProvider`/`SimulationConfigProvider` filter by region at the *bundle* level instead — comparing the requested `region` against `scenario.incident_request.region` and returning `[]` on a mismatch. Every bundled scenario is single-region today, so this is equivalent to per-row filtering in practice; it's the reason logs/config previously leaked cross-region data that metrics/routing correctly excluded (a request for a region that didn't match the scenario's data still got the scenario's full log/config set). The same fix applies in `mcp_telemetry_server/server.py`'s `get_logs`/`get_config_changes` tools, which back the MCP-mode adapters.

## Reporting Degraded Fetches

Every provider ABC (`providers/base.py`) declares a `degraded: bool = False` class-level default. Simulation providers never set it — there's nothing to fail. `providers/adapters/mcp_adapter.py`'s adapters set `self.degraded = True` in their `except` branch (resetting to `False` at the start of each call) whenever a fetch/parse failure caused the "never raise" contract to fall back to `[]`, so the flag reflects only the *most recent* call on that provider instance — callers must read it immediately after calling the fetch method, before any `await`, since a long-lived provider instance is reused across requests.

A real backend adapter (Prometheus/ELK/Splunk) implementing this contract should do the same: set `degraded = True` whenever falling back to an empty/partial result due to a transport or backend failure, and leave it `False` when a genuinely empty result reflects the query actually returning nothing. Domain agents read this flag right after calling the provider and set it on `AgentFinding.data_degraded`, which `agents/rca_synthesizer.py` uses to distinguish a verified-clean incident from one where telemetry simply couldn't be retrieved — see `project_docs/decision-policy.md`'s "Verified-Clean vs. Degraded-Data" section for the full rationale.

## Configuration Pattern

Use `config/config.yaml` to choose provider mode by domain.

Example strategy:

- Start hybrid: metrics real, others simulation.
- Validate output quality.
- Progressively move remaining domains to real adapters.

## Adapter Validation Checklist

1. Unit tests for field mapping.
2. Timeout and retry behavior tests.
3. Empty-result behavior test.
4. Partial-data behavior test.
5. End-to-end scenario run with adapter enabled.

## Rollout Strategy

1. Shadow mode
- Run adapter in parallel to simulation, compare findings.

2. Canary mode
- Enable adapter for subset of incidents/regions.

3. Full mode
- Switch provider in config after stability and quality thresholds are met.

## Baseline Provider

Domain agents that perform z-score anomaly detection require a `BaselineProvider` in addition to a telemetry provider.

### Interface

```python
# providers/base.py
class BaselineProvider(ABC):
    @abstractmethod
    def get_baseline(self, entity_key: str, metric: str) -> EntityBaseline | None:
        raise NotImplementedError
```

`EntityBaseline` fields (see `models/schemas.py`):

| Field | Type | Description |
|-------|------|-------------|
| `entity_key` | str | Identifies the entity, e.g. `"switch:C"`, `"region:us-east"`, `"component:api-gw"` |
| `metric` | str | Metric name, e.g. `"error_rate"`, `"throughput_gbps"`, `"change_count"` |
| `mean` | float | Historical mean value |
| `std_dev` | float | Historical standard deviation |
| `sample_count` | int | Number of samples used to compute the baseline |
| `last_updated` | datetime | When the baseline was last refreshed |
| `window_hours` | int | Lookback window used (default 24) |

### Simulation baseline

`providers/simulation/baseline_sim.py` provides `SimulationBaselineProvider` with hardcoded tables covering:

- `region:us-east` — `error_rate`, `packet_loss`, `throughput_gbps`, `change_count`
- `switch:A/B/C/D` — per-metric baselines
- `component:Switch-C eth0/1`, `component:api-gw`, `component:CORE-01` — `change_count`

### Prometheus baseline stub

`providers/adapters/prometheus_baseline_adapter.py` contains `PrometheusBaselineProvider` which raises `NotImplementedError`. Implement `get_baseline` using Prometheus recording rules or range queries for mean/std_dev over the desired window.

### Selecting the baseline provider

Set `baselines.provider` in `config/config.yaml`:

```yaml
baselines:
  provider: simulation   # only "simulation" is currently accepted — see below
  metrics_z_threshold: 3.0
  config_z_threshold: 2.5
  legacy_fallback: true
```

The value is validated at startup, and **`baselines.provider: prometheus` is now rejected** with a `ConfigValidationError` rather than accepted. It used to pass validation, which meant `PrometheusBaselineProvider()` got instantiated and then raised `NotImplementedError` on first real use inside the agent's request handler — the handler's own `try` swallowed that, so every baseline lookup silently fell back to the legacy hard-coded threshold with no visible failure anywhere. A config mode that can never actually work shouldn't pass startup validation, so `core/orchestrator.py::validate_config` now raises explicitly, naming that the adapter is an unimplemented stub. **You must actually implement `PrometheusBaselineProvider.get_baseline` and update `validate_config` to accept `"prometheus"` again** before this mode is usable — don't just revert the validation check.
