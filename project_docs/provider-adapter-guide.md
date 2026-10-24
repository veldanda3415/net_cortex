# Provider Adapter Guide

This guide explains how to connect a telemetry backend (CloudWatch, CloudTrail, Prometheus, a log store, …) to NetCortex without touching orchestration or agent logic.

## Architecture

```
RCA pod                                         telemetry pod(s)
┌──────────────────────────────┐   MCP        ┌──────────────────────────────────────┐
│ domain agents                │  contract v2 │ mcp_telemetry_server                 │
│   └ TelemetryRegistry ───────┼─────────────▶│   tools: get_metrics … get_topology  │
│       endpoints per domain   │              │   TelemetryService (fan-out)         │
│       retry · breaker · sess │              │     └ source plugins ── backend APIs │
└──────────────────────────────┘              └──────────────────────────────────────┘
        no cloud credentials                      credentials + IAM live here only
```

A new data source is a **source plugin inside a telemetry server**, never code in the RCA pod. The RCA pod only learns endpoint URLs from config. That gives three properties:

- **Least privilege.** Only the telemetry pod holds credentials and IAM.
- **Smaller blast radius.** A misbehaving backend integration can't take down the RCA process.
- **One fixed wire contract.** Every source emits the same envelope, specified in [CONTRACT.md](../CONTRACT.md).

## Writing a source plugin

Plugins live in `mcp_telemetry_server/plugins/`. Each one subclasses `SourcePlugin` (in `plugins/base.py`) and registers itself with a decorator:

```python
from contract import Coverage, Entity, Signal, TelemetryQuery
from mcp_telemetry_server.plugins.base import SourcePlugin, register_plugin


@register_plugin
class CloudWatchPlugin(SourcePlugin):
    name = "cloudwatch"                  # key under the `sources:` config section
    domains = frozenset({"metrics"})     # which tools fan out to this plugin
    needs_cloud_credentials = True       # refused in the RCA pod's in-process server

    def __init__(self, config=None):
        super().__init__(config)         # self.config = sources.cloudwatch.*
        # build clients here; fail fast on bad config with ValueError("ConfigValidationError: ...")

    async def fetch(self, query: TelemetryQuery, *, domain: str) -> tuple[list[Signal], Coverage]:
        rows, truncated = await self._read(query)    # your backend call(s)
        signals = [self._to_signal(r) for r in rows]
        return signals, self.coverage("partial" if truncated else "ok", truncated=truncated, lag_seconds=...)

    def health(self) -> dict:
        return {"status": "ok"}          # ok | degraded | down — shown on /health, gates /ready
```

Then import the module in `mcp_telemetry_server/plugins/__init__.py` so the decorator runs, and enable the plugin in the server's config.

Optional: if the source knows about entities and relationships, add `"topology"` to `domains` and implement `async topology(region, scenario_id) -> (entities, edges, coverage)`.

### Plugin rules

1. **Emit contract v2 signals only.** Map the backend's fields into `Signal` (see [CONTRACT.md](../CONTRACT.md#signal-envelope)):
   - Put the native metric or event name in `name` and the native resource in `entity`. Both are free strings, so don't invent enums.
   - Put everything else in `attributes`.
2. **Set `evidence_ref` on every signal.** It must be unique per signal and stable across reads, for example a CloudTrail `eventID` or `<log group>/<query id>`. The RCA side deduplicates on `(source, evidence_ref)`, and investigators use it to jump to the raw record.
3. **Respect the window exactly.** Return only signals with `start_time <= timestamp <= end_time` (or inside `baseline_window` when `include_baseline` is set). No hidden default windows. Use event time for `timestamp` and your read time for `observed_at`.
4. **Report coverage honestly.** This is what separates "no data" from "data unavailable":
   - Return `ok` with no signals when the backend answered and nothing matched.
   - Return `partial` when you hit a page or row limit (also set `truncated=True`) or when part of the read failed.
   - **Raise** on a hard failure. The service turns the exception into a `failed` coverage entry with the error text, and other plugins for the same domain are unaffected.
5. **Keep state out of requests.** A plugin instance serves every concurrent RCA task. Clients and connection pools are fine to keep on the instance; per-request results and flags are not.
6. **Never log secrets.** Read credentials from the pod's environment or IAM role (IRSA / instance profile), never from config files checked into git.
7. **Fail at startup, not at first use.** An enabled plugin that can't work, such as a missing required config key, should raise `ValueError("ConfigValidationError: ...")` from `__init__`.

Timeouts are applied for you. Each plugin call is wrapped in `sources.<name>.timeout_seconds` (default 10 s), and a plugin that times out gets a `failed` coverage entry.

### Bundled plugins

| Plugin | Domains | Status |
|---|---|---|
| `simulation` | metrics, logs, routing, config, topology | Implemented. Serves `simulation/scenarios.py`, rebased so each scenario's incident time lands on `end_time`. Ages are rounded to whole seconds. LogEvent/ConfigChange rows have no region, so they are filtered at bundle level. |
| `replay` | metrics, logs, routing, config, topology | Implemented. Reads recorded v2 fixtures (`fixtures_dir` or `fixtures`) and shifts every timestamp by `query.end_time − recorded_window.end`. Used for CI, and later for replaying recorded AWS sessions offline. See `tests/fixtures/replay/`. |
| `cloudwatch` | metrics | Skeleton |
| `cloudtrail` | config | Skeleton |
| `tgw_routes` | routing, topology | Skeleton |
| `flow_logs` | logs, metrics | Skeleton |
| `frr_ssm` | logs, routing | Skeleton |
| `prometheus` | metrics | Skeleton (moved here from `providers/adapters/`) |
| `elk`, `splunk` | logs | Skeleton (moved here from `providers/adapters/`) |

Skeletons (`SkeletonPlugin`) raise `NotImplementedError` and contain no boto3. Their docstrings list the API calls and IAM actions the real implementation will need, so the telemetry pod's IAM policy can be reviewed up front. Enabling a skeleton fails server startup with a clear error rather than serving guaranteed-failed coverage.

### Configuring sources (telemetry server)

```yaml
# config/telemetry.yaml   (python -m mcp_telemetry_server --config …, or $NETCORTEX_TELEMETRY_CONFIG)
sources:
  simulation:
    enabled: true
  replay:
    enabled: true
    fixtures_dir: tests/fixtures/replay
    domains: [metrics, routing]     # optional: narrow the plugin's domains
    timeout_seconds: 10
    required: false                 # optional source: failure doesn't degrade findings
```

Every tool fans out concurrently to all enabled plugins for its domain and returns one coverage entry per plugin. `/health` shows the status of each plugin. `/ready` returns 503 when none is usable.

## Connecting the RCA pod (endpoint registry)

`providers/factory.py` builds a `TelemetryRegistry` from config. Each domain lists one or more telemetry MCP endpoints:

```yaml
telemetry:
  defaults:
    timeout_seconds: 8
    retry: {max_attempts: 2, base_delay_seconds: 0.2, max_delay_seconds: 2.0}
    circuit_breaker: {failure_threshold: 3, reset_timeout_seconds: 30}
  domains:
    metrics:
      endpoints:
        - name: aws-telemetry
          url: http://netcortex-telemetry-aws:9001/mcp
          auth: bearer_env           # none | bearer_env (token_env) | oidc (not yet)
          token_env: TELEMETRY_TOKEN
          required: true
        - name: dc-telemetry         # adding a second source is config-only
          url: http://netcortex-telemetry-dc:9001/mcp
          required: false
```

For each domain fetch, the registry does the following:

- Calls all of the domain's endpoints concurrently.
- Merges their signals, deduplicated on `(source, evidence_ref)`.
- Returns **one coverage entry per endpoint**.

The agent copies `response.degraded` into `AgentFinding.data_degraded`. Degradation is computed from that request's coverage, never from provider state, because providers are shared across concurrent incidents.

Each endpoint gets these reliability controls, implemented in `providers/adapters/mcp_adapter.py` and `providers/resilience.py`:

- **Reused MCP session.** One session per endpoint, opened lazily and owned by a background task. All callers share it. It is reopened after a transport failure. There is no thread or event loop per call.
- **Per-attempt timeout.** Set with `timeout_seconds`.
- **Bounded retry with full jitter.** Applies to **transport errors only**: connection failures, timeouts, closed streams, HTTP 429/502/503/504. A tool error, such as a bad argument or a contract version mismatch, is not retried.
- **Circuit breaker.** It opens after `failure_threshold` consecutive transport failures. While open, calls fail immediately with "circuit open". After `reset_timeout_seconds` it lets one trial call through: success closes the breaker, failure reopens it.

Keep `timeout_seconds × max_attempts` comfortably below `a2a.analysis_timeout_seconds`, or the orchestrator will time the agent out before the registry can report coverage.

**`inproc://<name>` endpoints** run a real telemetry MCP server inside the RCA process, using the SDK's in-memory transport. Its sources come from `telemetry.inproc_sources`, which defaults to simulation. This is the zero-setup default for local runs and `eval`, and it still goes through MCP and the contract. Plugins with `needs_cloud_credentials = True` are refused there.

Configs without a `telemetry:` section still accept the old `providers:` / `mcp_endpoints:` keys, with a deprecation warning.

## Validation checklist for a new plugin

1. Unit tests mapping backend rows to `Signal`, covering `evidence_ref` uniqueness and window filtering.
2. A replay fixture recorded from the real backend (scrubbed), so CI exercises realistic shapes without cloud access.
3. A test for each coverage status: empty-but-ok, partial/truncated, and raising → `failed`.
4. `/health` reports something meaningful when the backend is down.
5. An end-to-end run with the plugin enabled in a telemetry pod and a second endpoint configured. Confirm that partial outages show up as degraded findings.

## Rollout strategy

1. **Shadow.** Add the new telemetry endpoint as `required: false` alongside the existing source, and compare findings.
2. **Canary.** Make it `required: true` for a subset of regions or incidents.
3. **Full.** Remove the old endpoint once quality thresholds are met. All three steps are config changes.

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
