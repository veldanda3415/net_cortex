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
| `get_signals` | any domain defined in the [detection catalog](#detection-catalog) | `ToolResponse` |
| `get_topology` | `topology` | `TopologyResponse` |

The domain tools all take the `TelemetryQuery` fields as flat arguments, plus `contract_version`. `get_signals` also takes `domain`, which must be a `telemetry_domain` in `domains.json`; it lets a new domain be served without a new tool. `get_topology` takes `region`, `scenario_id?` and `contract_version`.

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
| `domain` | A `telemetry_domain` from `domains.json` (`metrics`, `logs`, `routing`, `config`, …), or `topology` |
| `source` | The backend the signal came from (`cloudwatch`, `simulation`, …). Replay keeps the *recorded* source. |
| `attributes` | Free-form dict for anything else the source knows. |
| `evidence_ref` | Raw pointer back to the source record, such as a CloudTrail `eventID`, `<log group>/<query id>`, or `<command id>/<instance id>`. |

`name` and `entity.type` are deliberately **not** enums. The [detection catalog](#detection-catalog) gives known names their meaning and rules; a name the catalog doesn't know is still accepted and kept as unclassified evidence.

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

Each domain agent copies `response.degraded` into `AgentFinding.data_degraded`, and a degraded domain with no anomaly is never summarized as clean: its summary says the telemetry was incomplete and the domain is not verified clean. When every finding is clean but at least one is degraded, the synthesizer reports "RCA inconclusive" instead of a clean bill of health; see [project_docs/decision-policy.md](project_docs/decision-policy.md).

Degradation is computed **per request** from that request's coverage. Nothing is stored on the shared provider, so concurrent RCA tasks never see each other's failures.

## Topology: `TopologyResponse`

`{contract_version, region, entities: [Entity], relationships: [{type, source: Entity, target: Entity, attributes}], coverage}`

The documented relationship chains are:

- tunnel → vpn_connection → tgw_attachment → tgw_route_table
- security_group → eni → instance
- subnet → route_table
- vpc → vpc_peering → vpc

The simulation plugin returns a small static topology that covers all of them.

## Detection catalog

The catalog decides what the envelope's signals mean: which signals exist, how each is detected, which domains run, and how findings corroborate each other. It ships in the image at [`contract/catalog/`](contract/catalog/). Each file has a JSON Schema in [`contract/catalog/schema/`](contract/catalog/schema/).

| File | Holds | Read by |
|---|---|---|
| `entities.json` | `entity_types`, and `relationships` with their allowed source and target types | validation, topology consumers |
| `signals.json` | One entry per signal: `signal_type`, `unit`, `domain`, a one-line `description` (passed to the LLM synthesizer), optional `match_names` (other raw names it covers, e.g. CloudTrail event names), and one detection `rule` | domain agents |
| `domains.json` | Per domain: `telemetry_domain`, the MCP `tool` that feeds it, its agent card (name, port, skill), observation grouping, summary templates, confidence, `metrics_affected`, an optional relevance filter, and derived counts. Also the `degradation_map` (degradation type → active domains) | orchestrator, supervisor, agents, synthesizer |
| `correlation.json` | Cross-domain rules: when a finding is corroborated or contradicted by its peers, and the confidence adjustment and note to apply | orchestrator collaboration phase |
| `sources.json` | Mappings from raw source record fields to signals (entity, value, severity, attributes) | telemetry source plugins (simulation today) |

### Loading and validation

- **Override directory.** `$NETCORTEX_CATALOG_DIR` names an override directory. Each file there is merged onto the default **by key**: an override entry replaces the default entry with the same key as a whole (one signal, domain or rule at a time), and entries it doesn't mention are kept. In Kubernetes the override is the `netcortex-catalog-override` ConfigMap, mounted into both pods.
- **Three validation passes.** Validation runs at startup in the RCA process and in the telemetry server:
  1. JSON Schema, per file.
  2. The typed models.
  3. Cross-file references. For example, a signal's domain must exist, a mapping can only emit catalog signals, a summary template can only use known placeholders, and a correlation note must carry its idempotency marker.
- **Fails closed.** An invalid catalog stops the process, and the error names the file and field, e.g. `signals.json: signals.x.domain: unknown domain 'nope'`.
- **CI.** Run `python app/main.py catalog validate <override-dir>`, which exits 1 on error. `python deploy.py` runs the same check on the ConfigMap before applying anything, and stamps a catalog hash into both pod templates so a catalog change rolls the pods.

### Rule types (closed set)

Each signal has at most one `rule`. There is no expression language, and nothing is evaluated.

| `type` | Fires when | Notes |
|---|---|---|
| `threshold` | `op` `gt` / `lt` / `between` (`min`..`max`) holds for `field` | `field` is `value` (default) or `attributes.<name>` |
| `state_equals` | `field` equals `value` | e.g. `TunnelState` = 0, or `attributes.state` = `blackhole` |
| `zscore_baseline` | \|z\| > `threshold` | Uses `baseline_window` samples of the same series when there are at least `min_baseline_samples` of them; otherwise the baseline provider, trying `baseline_keys` in order (`switch=label:switch`, `region=region`, …); otherwise the optional threshold `fallback`, only if `baselines.legacy_fallback` is on |
| `rate_change` | value ≥ reference × `factor` (`increase`), or ≤ reference ÷ `factor` (`decrease`) | The reference is the mean of baseline-window samples, or else of earlier samples of the same series in the window. Ignored when the reference is ≤ `min_reference` |
| `event_presence` | `name` is in the allow-list (`names`, defaulting to the signal name plus `match_names`) and severity ≥ `min_severity` | |

A finding's `key_events` carry `rules_fired` (`signal`, `rule` id, `reason`, `evidence_ref`) and `evidence_refs`, so every claim traces back to a raw record.

**Unclassified evidence.** Signals whose name is not in the catalog are never an error. They are logged, kept in `AgentFinding.unclassified_evidence` and `RCAReport.unclassified_evidence`, and shown to the LLM synthesizer as unclassified evidence. No rule judges them.

**Relevance filter.** A domain can declare `relevance` (type `incident_text`) to discard hits on listed signals that don't relate to the incident description. Relevance means a shared token with the configured fields, or a symptom word in the description combined with a matching attribute value. The default catalog uses this for simulated config changes, which reproduces the previous config agent exactly.

### How to add a signal

1. Add an entry to `signals.json` in an override directory (or in the defaults). Give it `signal_type`, `domain` (an existing domain id), `unit`, a one-line `description`, and a `rule`. If several raw names mean the same thing (e.g. a family of CloudTrail write calls), list them in `match_names`.
2. Make sure a source emits signals with that `name`, either through an existing plugin's mapping in `sources.json` or as data recorded in a replay fixture.
3. Optionally, reference it from `correlation.json` (a `key_event` condition on its fields) or from a domain's summary variants (`when.hit_names_any`).
4. Run `python app/main.py catalog validate <dir>`, then a replay fixture through the engine.

Example override: [`tests/fixtures/catalog_overrides/latency_signal/`](tests/fixtures/catalog_overrides/latency_signal/) makes `latency_ms` a detection signal. Scenario 3's metrics finding flips to anomalous with no code change.

### How to add a domain

1. **Declare the domain.** In `domains.json`, add the domain with:
   - `telemetry_domain`: a new name, e.g. `dns`.
   - `tool: "get_signals"`.
   - An `agent` card with a free port and a unique skill id.
   - `summary` templates, `confidence` and `metrics_affected`.
2. **Activate it.** Add the domain to the `degradation_map` lists where it should run (at least `network` and `unknown`).
3. **Add its signals** to `signals.json`, with `domain` set to the new domain id. Add any new entity types to `entities.json`.
4. **Point a telemetry source at it.** Either enable `replay` with fixtures whose signals carry `"domain": "<telemetry_domain>"`, or add a plugin mapping. Configure the RCA endpoint under `telemetry.domains.<telemetry_domain>.endpoints`; without one, it defaults to `inproc://local`.
5. **Validate**, then run an incident. The RCA process starts one more agent for the domain, the orchestrator dispatches to it, and the report includes its finding.

Worked example, with zero code: [`tests/fixtures/catalog_overrides/dns/`](tests/fixtures/catalog_overrides/dns/) plus [`tests/fixtures/replay_dns/`](tests/fixtures/replay_dns/) add a `dns` domain with an NXDOMAIN-spike rule. `tests/integration/test_catalog_overrides.py` shows the DNS finding in the RCA report.

### What still requires code

- **A new data source.** A telemetry source plugin, such as the CloudWatch/CloudTrail/TGW/Flow Logs/FRR skeletons, plus its credentials and IAM. Once written, its field mapping can live in `sources.json`.
- **A new rule type.** Add it to `contract/catalog_models.py` and `detection/rules.py`. This is deliberate: the set stays closed and reviewable.
- **A new MCP tool.** Today these are `get_metrics`, `get_logs`, `get_routing_events`, `get_config_changes`, `get_signals` and `get_topology`. New domains use `get_signals`, so they don't need one.
- **New agent reasoning.** Anything the generic pipeline can't express: new correlation condition types, summary placeholders, relevance filter types, or LLM prompting. The default catalog's `incident_text` relevance filter is an example of reasoning that lives in code but is configured by data.
