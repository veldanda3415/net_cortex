# Telemetry Contract v2

This is the wire contract between the NetCortex RCA pod and the telemetry MCP servers it reads from. The pydantic models live in [`contract/models.py`](contract/models.py), and the exported JSON Schema lives in [`contract/schema/`](contract/schema/). Regenerate the schema with `python -m contract.export_schema`; a test fails if the files go stale.

```
RCA pod ──MCP (Streamable HTTP, or inproc:// for local runs)──▶ telemetry server ──▶ source plugins
 agents   registry: endpoints per domain                          tools                cloudwatch, replay, …
```

The RCA pod only speaks MCP. Cloud credentials and IAM live only in telemetry pods.

## Versioning

`contract_version` is `"<major>.<minor>"` and is currently `"2.0"`.

- **Minor** bumps are additive: they add new optional fields only. Peers accept any minor version of their own major.
- **Major** bumps are breaking. A telemetry server rejects a request whose major version differs from its own, with a tool error such as `Unsupported contract_version '1.0': this peer speaks major version 2 (2.0)…`. The RCA client applies the same check to every response it receives. Either way, the mismatch shows up as a `failed` coverage entry for that endpoint, so the finding is marked degraded instead of silently empty.

## Tools

| Tool | Domain | Returns |
|---|---|---|
| `get_metrics` | `metrics` | `ToolResponse` |
| `get_logs` | `logs` | `ToolResponse` |
| `get_routing_events` | `routing` | `ToolResponse` |
| `get_config_changes` | `config` | `ToolResponse` |
| `get_topology` | `topology` | `TopologyResponse` |

The four domain tools all take the `TelemetryQuery` fields as flat arguments, plus `contract_version`. `get_topology` takes `region`, `scenario_id?` and `contract_version`.

The server also exposes two HTTP routes:

- `GET /health` reports the status of each plugin.
- `GET /ready` returns 503 when no enabled plugin is usable.

## Query: `TelemetryQuery`

| Field | Type | Notes |
|---|---|---|
| `region` | string | Accepts `us-east` and AWS-style `us-east-1`, `us-gov-west-1`, … |
| `start_time`, `end_time` | ISO-8601 datetime | Inclusive on both ends. Naive times are treated as UTC. `start_time <= end_time`. |
| `entity_filters` | list of `{type?, id?, labels?}` | Optional. A signal is kept if its entity matches **any** filter, where all fields set on a filter must match. |
| `include_baseline` | bool | Asks the server to also return baseline-window data (see `baseline_window`). |
| `scenario_id` | int | Used by the simulation plugin only. Every other source ignores it. |

**Window anchoring.** The orchestrator always queries `end = incident.reported_at` and `start = end − simulation.window_minutes`. `reported_at` is the alarm time:

- The webhook normalizer keeps a caller-supplied `reported_at`, `alarm_time` or CloudWatch `StateChangeTime`.
- `run_rca` accepts `reported_at`.
- If no alarm time is supplied, ingest time is used.

This way, a webhook that arrives late still queries the window around the alarm.

## Response: `ToolResponse`

| Field | Notes |
|---|---|
| `contract_version` | `"2.0"` |
| `window` | `{start, end}`: the window that was actually queried. |
| `baseline_window` | Optional `{start, end}`. Set when `include_baseline` was requested. Signals in it may precede `window.start`. |
| `coverage` | One entry per source. See [Coverage](#coverage). |
| `signals` | List of `Signal`. |
| `next_cursor` | Optional. Set when a source has more data than it returned. The RCA side does not follow cursors yet; it marks that endpoint's coverage `truncated`. |

## Signal envelope

| Field | Notes |
|---|---|
| `signal_type` | `metric` \| `event` \| `state` |
| `timestamp` | **Event time**: when it happened at the source. |
| `observed_at` | **Ingest time**: when the telemetry server read it. `observed_at − timestamp` is the source lag. |
| `entity` | `{type, id, labels}`. `type` is a free string (`tunnel`, `tgw_route_table`, `switch`, …). |
| `name` | Free string (`TunnelState`, `error_rate`, `ModifyVpnConnection`, …). |
| `value`, `unit` | Optional. Numeric signals only. |
| `severity` | `debug` \| `info` \| `warning` \| `error` \| `critical` |
| `domain` | `metrics` \| `logs` \| `routing` \| `config` \| `topology` |
| `source` | The backend the signal came from (`cloudwatch`, `simulation`, …). Replay keeps the *recorded* source. |
| `attributes` | Free-form dict for anything else the source knows. |
| `evidence_ref` | Raw pointer back to the source record, such as a CloudTrail `eventID`, `<log group>/<query id>`, or `<command id>/<instance id>`. |

`name` and `entity.type` are deliberately **not** enums. A later catalog will validate them. Until then, any string is accepted.

**Deduplication.** When several endpoints serve the same domain, the RCA registry merges their signals and drops duplicates on `(source, evidence_ref)`. A signal without `evidence_ref` is never deduplicated. A source must therefore emit an `evidence_ref` that is unique per signal and stable across reads.

## Coverage

Each coverage entry says how completely one source answered:

| Field | Notes |
|---|---|
| `source` | On the server, the plugin name. In the RCA-side merged response, the endpoint name. |
| `status` | `ok`: everything requested was read. `partial`: some data was missing (page limit, an unreadable log group, malformed rows skipped). `failed`: nothing could be read. |
| `lag_seconds` | How far behind real time the source is, if known. |
| `truncated` | `true` when a row or page limit was hit. |
| `error` | A human-readable reason when `status != ok`. |
| `required` | Default `true`. If `false`, a failure of this source does not make the response degraded. Use it for optional enrichment sources. |

The RCA registry collapses each endpoint's per-source coverage into **one entry per endpoint**:

- The status is `ok` if every source was ok, `failed` if all sources failed, and `partial` otherwise.
- Errors are concatenated.
- `lag_seconds` is the largest lag of any source.
- `truncated` is true if any source was truncated or the endpoint returned a `next_cursor`.

An endpoint that cannot be reached (timeout, refused connection, open circuit, version mismatch) gets a `failed` entry.

The response-level helpers are:

- `ToolResponse.status` is `ok` if every entry is ok, `failed` if every entry failed or there are no entries, and `partial` otherwise.
- `ToolResponse.degraded` is true when any **required** entry is not `ok`, or when there is no coverage at all.

## "No data" vs. "data unavailable"

These two cases must never look the same:

| Situation | `signals` | `coverage` | `degraded` | What it means |
|---|---|---|---|---|
| Source answered and nothing happened | `[]` | all `ok` | `false` | **Verified no data.** It is safe to report "no anomaly". |
| Source answered with part of the data | some | `partial` | `true` | **Some data unavailable.** Analyze what arrived, but don't trust its absence. |
| Source could not be read | `[]` | `failed` | `true` | **Data unavailable.** "No anomaly" cannot be verified. |
| Optional source failed, required sources OK | any | `partial` | `false` | Enrichment missing. The core verdict still stands. |

Each domain agent copies `response.degraded` into `AgentFinding.data_degraded`. When every finding is clean but at least one is degraded, the synthesizer reports "RCA inconclusive" instead of a clean bill of health; see [project_docs/decision-policy.md](project_docs/decision-policy.md).

Degradation is computed **per request** from that request's coverage. Nothing is stored on the shared provider, so concurrent RCA tasks never see each other's failures.

## Topology: `TopologyResponse`

`{contract_version, region, entities: [Entity], relationships: [{type, source: Entity, target: Entity, attributes}], coverage}`

The documented relationship chains are:

- tunnel → vpn_connection → tgw_attachment → tgw_route_table
- security_group → eni → instance
- subnet → route_table
- vpc → vpc_peering → vpc

The simulation plugin returns a small static topology that covers all of them.

## Compatibility note (temporary)

In this release, domain agents still analyze the legacy `MetricSnapshot` / `LogEvent` / `RoutingEvent` / `ConfigChange` models. [`providers/compat.py`](providers/compat.py) translates v2 signals into those models.

Signals that don't fit the old closed shapes are skipped by that translation. Examples are unknown metric names, change types outside the old literals, and metric groups missing a legacy field. Those signals are still present in the `ToolResponse`; the agents just can't see them yet. The compat layer will be removed once agents consume signals directly.
