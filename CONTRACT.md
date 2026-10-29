# Telemetry Contract v2

This is the wire contract between the NetCortex RCA pod and the telemetry MCP servers it reads from. The pydantic models live in [`contract/models.py`](contract/models.py), and the exported JSON Schema lives in [`contract/schema/`](contract/schema/). Regenerate the schema with `python -m contract.export_schema`; a test fails if the files go stale.

```
RCA pod ──MCP (Streamable HTTP, or inproc:// for local runs)──▶ telemetry server ──▶ source plugins
 agents   registry: endpoints per domain                          tools                cloudwatch, replay, …
```

The RCA pod only speaks MCP. Cloud credentials and IAM live only in telemetry pods.

## Versioning

`contract_version` is `"<major>.<minor>"` and is currently `"2.1"`. 2.1 added the optional `TelemetryQuery.anchor_time`, which the RCA side always sends: upgrade telemetry servers before the RCA pod, since a 2.0 server does not know the argument.

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
| `scenario_id` | int | Used by the simulation plugin, and by replay fixtures that declare one (a fixture with `scenario_id` answers only that scenario). Every other source ignores it. |
| `anchor_time` | ISO-8601 datetime | Optional (2.1). The alarm instant the query belongs to. Live sources ignore it. Replay aligns a recording's `alarm_at` to it, so queries for the minutes after the alarm line up with the recording. |

**Window anchoring.** The orchestrator always queries `end = incident.reported_at` and `start = end − simulation.window_minutes`. `reported_at` is the alarm time:

- The webhook normalizer keeps a caller-supplied `reported_at`, `alarm_time` or CloudWatch `StateChangeTime`.
- `run_rca` accepts `reported_at`.
- If no alarm time is supplied, ingest time is used.

This way, a webhook that arrives late still queries the window around the alarm.

Two more queries hang off the same anchor:
- **Is it still happening?** When the alarm is at least a minute old, each agent also reads `[reported_at, reported_at + 15 min]`, capped at now. That only decides whether hit entities are still firing; it never creates findings.
- **Peer questions.** A `validation_request` is scoped to one entity (`entity_filters` with the topology's aliases for it) and starts `walk.lookback_minutes` before the window or the first symptom, whichever is earlier.

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

`entities.json` lists every relationship type; collaboration walks them (see "How Peer Collaboration Actually Works" in `project_docs/agent-authoring-guide.md`). Entities match by id or by any label value, so a tunnel named by its outside IP in CloudWatch is the same node as `vpn-…-tunnel-1` in the topology. The original relationship chains are:

- tunnel → vpn_connection → tgw_attachment → tgw_route_table
- security_group → eni → instance
- subnet → route_table
- vpc → vpc_peering → vpc

The simulation plugin returns a small static topology that covers all of them. Its entities are not the simulation scenarios' entities, so simulation runs have nothing to walk. `aws_topology` reads the real one from describe calls, and replay fixtures carry their own.

## Detection catalog

The catalog decides what the envelope's signals mean: which signals exist, how each is detected, which domains run, and how findings corroborate each other. It ships in the image at [`contract/catalog/`](contract/catalog/). Each file has a JSON Schema in [`contract/catalog/schema/`](contract/catalog/schema/).

| File | Holds | Read by |
|---|---|---|
| `entities.json` | `entity_types`, and `relationships` with their allowed source and target types and which end depends on the other (`dependent`: `source`, `target` or `both`) | validation, collaboration (topology walk, ranking) |
| `signals.json` | One entry per signal: `signal_type`, `unit`, `domain`, a one-line `description` (passed to the LLM synthesizer), optional `match_names` (other raw names it covers, e.g. CloudTrail event names), and one detection `rule` | domain agents |
| `domains.json` | Per domain: `telemetry_domain`, the MCP `tool` that feeds it, its agent card (name, port, skill), observation grouping, summary templates, confidence, `metrics_affected`, an optional relevance filter, and derived counts. Also the `degradation_map` (degradation type → active domains) | orchestrator, supervisor, agents, synthesizer |
| `correlation.json` | Cross-domain rules: when a finding is corroborated or contradicted by its peers, and the confidence adjustment and note to apply | orchestrator collaboration phase |
| `sources.json` | Mappings from raw source record fields to signals (entity, value, severity, attributes) | telemetry source plugins (simulation today) |
| `investigation.json` | How collaboration investigates: `probes` (entity type → domains that can be asked about it), `signal_roles` (cause or symptom), `lead_patterns` (regex → entity type, for ids named in evidence), `walk` (`max_hops`, `lookback_minutes`, `max_requests_per_round`) and `rank_weights` | domain agents (questions), orchestrator (ranking) |

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
| `threshold` | `op` `gt` / `lt` / `between` (`min`..`max`) holds for `field` | `field` is `value` (default) or `attributes.<name>`. Optional `scale` multiplies it, and `per` divides it by another field (`labels.capacity_bps` turns traffic into utilization; no divisor, no verdict) |
| `state_equals` | `field` equals `value` | e.g. `TunnelState` = 0, or `attributes.state` = `blackhole` |
| `zscore_baseline` | \|z\| > `threshold` | Uses `baseline_window` samples of the same series when there are at least `min_baseline_samples` of them; otherwise the baseline provider, trying `baseline_keys` in order (`switch=label:switch`, `region=region`, …); otherwise the optional threshold `fallback`, only if `baselines.legacy_fallback` is on |
| `rate_change` | value ≥ reference × `factor` (`increase`), or ≤ reference ÷ `factor` (`decrease`) | The reference is the mean (or, with `reference: median`, the median, which resists bursty series) of baseline-window samples, or else of earlier samples of the same series in the window. Ignored when the reference is ≤ `min_reference` |
| `event_presence` | `name` is in the allow-list (`names`, defaulting to the signal name plus `match_names`) and severity ≥ `min_severity` | |

A finding's `key_events` carry `rules_fired` (`signal`, `rule` id, `reason`, `evidence_ref`) and `evidence_refs`, so every claim traces back to a raw record. Repeats of one line on one entity collapse into one key event with a `count` (numbers in the text are ignored when comparing), and cause-role signals come first, so a burst of generic lines cannot crowd out the decisive one. `AgentFinding.evidence` keeps every rule hit, deduplicated the same way (up to 50 rows), and is what collaboration, ranking, the LLM and the drill scorer read.

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

### Recipes

Each recipe below is an override directory you can copy as-is. [`tests/integration/test_catalog_recipes.py`](tests/integration/test_catalog_recipes.py) runs every one through the real engine, before and after the override, and fails if a snippet on this page drifts from its fixture.

Two rules apply to every recipe:

- **An override entry replaces the default entry with the same key as a whole.** To change one field of an existing signal, copy the whole default entry from `contract/catalog/` and edit it.
- **A signal's raw `name` must reach the catalog.** A catalog key or a `match_names` entry has to equal the `name` a source emits. Until one does, the signal shows up in `unclassified_evidence` and no rule judges it. Use that to find which names to add: run the incident, then read `unclassified_evidence` in `rca_report.json`.

#### Treat WARN log lines as anomalous

The default `log_message` rule only fires at `error` and above. Lowering the severity floor makes scenario 2's `WARN BGP session flap` line anomalous.

<!-- recipe: tests/fixtures/catalog_overrides/recipes/warn_logs/signals.json -->
```json
{
  "signals": {
    "log_message": {
      "signal_type": "event", "domain": "log",
      "description": "Application/network log line; WARN and above are anomalous.",
      "rule": {"type": "event_presence", "min_severity": "warning"}
    }
  }
}
```

#### Detect a new named error, such as a syslog mnemonic

Use this when a source emits each error type under its own `name`, such as a syslog mnemonic, an FRR event or a CloudTrail event name. List every raw name the signal covers in `match_names`. Unless the rule's `names` narrows it, `event_presence` fires on the signal's own key plus all of its `match_names`.

<!-- recipe: tests/fixtures/catalog_overrides/recipes/syslog_link_down/signals.json -->
```json
{
  "signals": {
    "link_down": {
      "signal_type": "event", "domain": "log",
      "match_names": ["LINK-3-UPDOWN"],
      "description": "Syslog from a network device: an interface changed state to down.",
      "rule": {"type": "event_presence", "min_severity": "error"}
    }
  }
}
```

With [`tests/fixtures/replay_recipes/syslog_link_down.json`](tests/fixtures/replay_recipes/syslog_link_down.json) replayed:

- the `LINK-3-UPDOWN` line fires `link_down.event_presence`, and its `evidence_ref` is in the key event;
- the uncatalogued `SYS-5-CONFIG_I` line stays in `unclassified_evidence`.

**When the error is only in the message text.** If a source emits every line under one generic name (simulation emits all logs as `log_message`), a rule can't tell one error from another: rules match on `name` and severity, never on message text. You have two options:

- **Give each error type its own name at the source.** Do this in the plugin that builds the signals, then use this recipe. This is a code change in the plugin.
- **Use the text for corroboration only.** A correlation `key_event` condition can test a peer's `message` with `contains` (see the corroboration recipe below). It adjusts confidence, but it can't make a finding anomalous.

#### Add an AWS state metric (Direct Connect `ConnectionState`)

A new state check is a `state_equals` rule. A new entity type goes in `entities.json` in the same override directory.

<!-- recipe: tests/fixtures/catalog_overrides/recipes/dx_connection_down/entities.json -->
```json
{
  "entity_types": {
    "dx_connection": {"description": "AWS Direct Connect dedicated or hosted connection"}
  }
}
```

<!-- recipe: tests/fixtures/catalog_overrides/recipes/dx_connection_down/signals.json -->
```json
{
  "signals": {
    "ConnectionState": {
      "signal_type": "metric", "domain": "metrics", "unit": "None", "entity_types": ["dx_connection"],
      "description": "AWS/DX ConnectionState: 1 when the Direct Connect connection is up, 0 when down.",
      "rule": {"type": "state_equals", "value": 0, "reason": "Direct Connect connection down (ConnectionState=0)"}
    }
  }
}
```

With [`tests/fixtures/replay_recipes/dx_connection_down.json`](tests/fixtures/replay_recipes/dx_connection_down.json) replayed, only the sample with value 0 fires.

The other single-signal shapes follow the same pattern:

- **A fixed limit:** `{"type": "threshold", "op": "gt", "value": 200}`. See `latency_signal/` above.
- **A deviation from baseline:** `zscore_baseline`.
- **A spike or collapse against the signal's recent level:** `rate_change` with `direction` and `factor`. See `TunnelDataIn` in the defaults and the `dns` example.

#### Add a cross-domain corroboration rule

A finding's confidence goes up when a peer's evidence matches. In scenario 4, the throughput drop on LAG0 is corroborated by the log agent's `LACP timeout` line.

<!-- recipe: tests/fixtures/catalog_overrides/recipes/lacp_corroboration/correlation.json -->
```json
{
  "rules": {
    "metrics.lacp_error_on_lag": {
      "domain": "metrics", "priority": 20,
      "description": "A metrics anomaly plus an LACP error in the logs points at a LAG member failure.",
      "when": {
        "self_anomaly": true,
        "all": [{"key_event": {"scope": "peers", "domains": ["log"], "field": "message", "op": "contains", "value": "LACP"}}]
      },
      "effect": {
        "kind": "corroboration",
        "confidence": {"op": "at_least", "value": 0.9},
        "note": "Peer corroboration: log agent reported an LACP error; metrics confidence elevated to 0.90"
      }
    }
  }
}
```

How the rule fields behave:

- **Order.** Rules for a domain are tried in `priority` order, lowest first, and only the first match applies.
- **Marker.** The `note` must start with `Peer corroboration:` (or `Peer contradiction:` for `kind: contradiction`). The marker stops the rule from applying twice across collaboration rounds.
- **Fields a `key_event` condition can test:** anything a key event carries, which means the signal's metric value under its name, its attributes (such as `message` and `change_type`), `entity_type` and `entity_id`.

### What still requires code

- **A new data source.** A telemetry source plugin, plus its credentials and IAM. The AWS plugins in `mcp_telemetry_server/plugins/aws/` are the worked example; an Azure, GCP or on-prem backend is a sibling package. New log formats or error classes on an existing log group are config (`cloudwatch_logs` queries), not code.
- **A new rule type.** Add it to `contract/catalog_models.py` and `detection/rules.py`. This is deliberate: the set stays closed and reviewable.
- **A new MCP tool.** Today these are `get_metrics`, `get_logs`, `get_routing_events`, `get_config_changes`, `get_signals` and `get_topology`. New domains use `get_signals`, so they don't need one.
- **Detecting on log message text.** Rules match a signal's `name` and severity, never its free text, so there is no substring or regex detection. The workaround is to name the error at the source (see the recipes above). A `message_match` option on `event_presence` would be a new rule capability.
- **Conditions over several signals within one domain.** "A and B together", "A before B" and "the ratio of A to B" can't be detected. Each rule judges one signal; a derived per-entity count (`domains.json` `derived`) is the only aggregation. Across domains, `correlation.json` can combine evidence, but only to adjust confidence.
- **New agent reasoning.** Anything the generic pipeline can't express: new correlation condition types, summary placeholders, relevance filter types, or LLM prompting. The default catalog's `incident_text` relevance filter is an example of reasoning that lives in code but is configured by data.
