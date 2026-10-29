# Agent Authoring Guide

This guide explains how to build or replace a NetCortex domain agent while preserving compatibility with the orchestration engine.

> Most new domains no longer need a hand-written agent. Add the domain to the detection catalog and the generic agent (`agents/domain_agent.py`) runs it; see "How to add a domain" in `CONTRACT.md`. Write a custom agent only for reasoning the catalog can't express.

## Purpose

A NetCortex agent should answer one question clearly:

- What does this domain believe is happening right now?
- How confident is that conclusion?
- What evidence supports that conclusion?

Every built-in and third-party agent must emit the same output contract (`AgentFinding`) so the orchestrator, collaboration loop, and synthesizer can reason consistently.

## Compatibility Contract

At startup, the registry expects each agent endpoint to expose:

1. Agent Card at `/.well-known/agent.json`
2. Analysis skill (`analyze-<domain>`)
3. Peer-response skill (`respond-to-peer`)
4. Schema contract with `outputSchema=AgentFinding`

If any of these are missing, registration fails (`AgentRegistry.register_from_card`).

Note on `respond-to-peer`: this skill carries the whole collaboration protocol (below). An agent that only acknowledges it still works: the orchestrator then revises that agent's finding itself from `correlation.json`, but the agent neither asks nor answers questions.

## Agent Decision Pipeline

Implement this 6-step pattern in every domain:

1. Input acquisition
- Pull telemetry for the region and the incident window via the shared `TelemetryRegistry`. Get it with `create_telemetry_provider(cfg, domains=(...))` from `providers/factory.py`; it calls every endpoint configured under `telemetry.domains.<domain>.endpoints`. Build the query with `providers/query.py::query_from_request(data)`. The window comes from the orchestrator's `start_time`/`end_time`, which are anchored on `incident.reported_at`.
- Return deterministic data shape. The built-in agents analyze contract v2 signals directly with `detection.analyze_domain`, driven by the detection catalog.
- Propagate `response.degraded` into `AgentFinding.data_degraded` (see "Reporting Degraded Data" below). A fetch failure must not silently look like "no anomaly."

2. Quality checks
- Validate required fields.
- Compute data quality score (freshness, completeness, cardinality).

3. Feature extraction
- Convert raw input to domain features.
- Keep feature names stable and explicit.

4. Anomaly detection
- Compare against baseline/rules/model output.
- Determine anomaly yes/no with explicit threshold logic.

5. Hypothesis generation
- Write one concise summary sentence.
- Include scope, timing, and affected component.

6. Confidence scoring
- Use deterministic formula in agent code.
- Down-weight confidence for poor data quality.

## Recommended Per-Agent Inputs and Features

### Metrics Agent
Inputs:
- Error rate
- Packet loss
- Throughput
- Latency
- Region and entity tags

Features:
- Baseline deviation (z-score or MAD)
- Change-point magnitude
- Multi-metric agreement score
- Blast radius (how many entities impacted)

### Log Agent
Inputs:
- Structured logs/events
- Severity levels
- Service identifiers

Features:
- Error/fatal rate lift vs baseline
- Event novelty score
- Temporal correlation to incident start
- Service concentration score

### Routing Agent
Inputs:
- Routing events (BGP/IGP)
- Path changes
- Link/path utilization signals

Features:
- Path delta (before vs after)
- Convergence duration
- Prefix/path scope affected
- Congestion correlation index

### Config Agent
Inputs:
- Config diffs
- Deploy/policy events
- Change metadata

Features:
- Change risk score (known risky change types)
- Component overlap with impacted nodes
- Time proximity to incident onset
- Rollback indicator

## Output Authoring Rules

AgentFinding fields should be interpreted as follows:

- `summary`: one sentence, domain-specific, evidence-grounded.
- `anomaly_detected`: strict boolean based on rule/model output.
- `key_events`: short list of top evidence objects (not full raw payload).
- `start_time`, `end_time`: analysis window used by this finding — always derive these from real telemetry timestamps. Falling back to `datetime.now()` for both when the primary evidence set is empty corrupts chronological ordering in the causal chain; prefer falling back to a broader-but-real timestamp source (see `config_agent.py`'s `time_source` fallback to the full `changes` list) before ever synthesizing `start_time == end_time == now`.
- `confidence`: numeric confidence from deterministic formula.
- `revised` and `revision_count`: collaboration-phase updates, set by `reconsider_finding` (see below) — never touched by the `/a2a` handler itself.
- `data_degraded`: `True` when this domain's telemetry fetch failed or degraded (never touched by the fetch itself returning genuinely empty data). See "Reporting Degraded Data" below.

### Example AgentFinding

```json
{
	"agent_id": "routing",
	"domain": "routing",
	"anomaly_detected": true,
	"summary": "Path change rerouted us-east-core via congested backup link.",
	"key_events": [
		{
			"path_id": "us-east-core",
			"change_type": "reroute",
			"details": "A->B->D changed to A->C->D"
		}
	],
	"start_time": "2026-05-05T10:00:00Z",
	"end_time": "2026-05-05T10:30:00Z",
	"confidence": 0.84,
	"revised": false,
	"revision_count": 0,
	"data_degraded": false
}
```

## Confidence Template

Use this template to keep behavior predictable:

- `signal_strength`: normalized anomaly magnitude in [0,1]
- `evidence_quality`: data completeness/freshness in [0,1]
- `consistency`: internal feature agreement in [0,1]

Formula:

`confidence = 0.5 * signal_strength + 0.3 * evidence_quality + 0.2 * consistency`

Clamp to [0.05, 0.99].

## How Peer Collaboration Actually Works

Collaboration follows the round model in `net_cortex_impl_v4.md` section 3: collect, then revise once. It runs for at most `a2a.max_iterations` rounds (2). Every message goes through `respond-to-peer`, keyed by `message_type`. The orchestrator routes the messages and holds the round barrier; the reasoning happens in the agents. The generic agent (`agents/domain_agent.py`) implements all of it with `detection/investigate.py`.

| message_type | Who sends it | What the receiving agent does | Reply |
|---|---|---|---|
| `finding_publish` | each agent's finding, to every peer | stores the peer's finding for the round; once every peer in `expect_peers` has published, plans its questions | `{"ack": true, "requests": [...]}` |
| `validation_request` | an agent's question, routed to the domain it asks | re-runs its own detection on a query scoped to the entity (`entity_filters`, with the topology's aliases) and an earlier window | `validation_response`: `verdict` (supported, refuted or inconclusive), the scoped `finding`, `leads` |
| `validation_response` | the answer, routed back to the asker | keeps the `leads` for its next round's questions | ack |
| `finding_update` | end of the round, to every agent | revises its finding once: evidence from the questions it answered (`via: validation`), then `correlation.json` | the revised `AgentFinding` |

**How an agent picks its questions** (`plan_requests`):
1. **Anchors.** It starts from the entities of its own rule-fired evidence, the leads it was handed, and the alarm's entities when its domain can see their type.
2. **Walk.** From each anchor it walks the topology (`get_topology`) up to `walk.max_hops`. It keeps only entities the anchor depends on, following each relationship's `dependent` end in `entities.json`.
3. **Ask.** For each such entity it asks every other domain that `investigation.json` `probes` can see it with, unless that domain already has evidence on it or was asked already.
4. **Window.** The question's window starts `walk.lookback_minutes` before the earlier of the window start and the first symptom. A cause older than the analysis window (a DNS record deleted before its TTL ran out) is still found.

**Answers never bypass the rules.** A scoped answer is the same `analyze_domain` on less data. The only difference is `scoped=True`, which skips the incident-text relevance filter: the entity was asked about explicitly, so a change on it counts even when the alarm text doesn't mention it.

**Agents keep a small per-incident session** (bounded, oldest dropped first): their own finding, the peers' findings per round, the answers they gave, and their leads. An agent that loses its session (restart) just acks, and the orchestrator falls back to revising it locally.

Practical guidance for a replacement agent:

- **Minimum:** answer `respond-to-peer` with an ack. You get Phase 0 behavior: corroboration from `correlation.json`, no questions.
- **Answering questions:** handle `validation_request` by scoping your query to `entity_filters` and the request's `start_time`/`end_time`. Reply with `request_id`, `verdict`, `finding` and `leads`. This alone lets other agents find causes in your domain.
- **Asking questions:** reply to the last expected `finding_publish` with `requests` shaped like `plan_requests` output: `request_id`, `target`, `entity`, `entity_filters`, `start_time`, `end_time`, `question`, `sender`.
- **Revising:** handle `finding_update` by returning your revised `AgentFinding`. Keep revision idempotent: guard it on the marker strings (`Peer corroboration:`, `Peer validation:`), because the convergence check compares summaries between rounds.

## Testing Checklist for New Agents

1. Healthy-case test returns `anomaly_detected=false`.
2. Clear anomaly test returns `anomaly_detected=true`.
3. Low-quality input lowers confidence.
4. Output schema validates as AgentFinding.
5. `respond-to-peer` returns a valid JSON-RPC ack result.
6. Your domain's correlation rules (via `detection.reconsider`, called directly, not through the endpoint) flip `revised=true` and increments `revision_count` on first call, and is a no-op on a second call with the same peer findings (idempotency).
7. A telemetry failure results in `AgentFinding.data_degraded=True`, not just an empty or no-anomaly finding. To test this, point an endpoint at an unreachable URL or use a source plugin that raises; see `tests/integration/test_provider_connectivity.py`.

## Baseline-Aware Anomaly Detection

Metrics and config agents use per-entity, per-metric baselines instead of global hard-coded thresholds.

### How it works

1. Each analysis request resolves an `EntityBaseline` for the entity being evaluated.
2. `compute_z_score(value, baseline)` produces the deviation in standard-deviation units.
3. `is_anomalous(value, baseline, z_threshold)` returns `True` when the z-score exceeds the configured threshold.
4. When no baseline exists for an entity, the agent falls back to hard-coded thresholds if `legacy_fallback: true` in `config.yaml`, or reports no anomaly otherwise.

### Baseline utility location

```
providers/baseline_utils.py   # compute_z_score, is_anomalous
providers/base.py             # BaselineProvider abstract class
providers/simulation/baseline_sim.py  # SimulationBaselineProvider (per-entity tables)
providers/adapters/prometheus_baseline_adapter.py  # PrometheusBaselineProvider stub — NOT selectable, see below
```

### Z-score thresholds (configurable in `config/config.yaml`)

```yaml
baselines:
  provider: simulation          # only "simulation" is accepted — see note below
  metrics_z_threshold: 3.0     # flag metric as anomalous when |z| > 3.0
  config_z_threshold: 2.5      # flag config change count as anomalous when |z| > 2.5
  legacy_fallback: true        # fall back to hard-coded thresholds when no baseline exists
```

Both thresholds are validated > 0 at startup. Invalid values cause fail-fast startup.

**`baselines.provider: prometheus` is rejected at startup**, not silently accepted. `PrometheusBaselineProvider.get_baseline()` is an unimplemented stub (raises `NotImplementedError`); `core/orchestrator.py::validate_config` used to accept `"prometheus"` as a config-valid value, which meant the provider got instantiated, then quietly degraded every baseline lookup to the legacy-threshold fallback on first use, with no failure surfaced anywhere. `validate_config` now raises `ConfigValidationError` naming this explicitly if you set `baselines.provider: prometheus` — implement `PrometheusBaselineProvider.get_baseline` for real before that mode becomes usable again.

### Confidence scaling

Confidence is now linked to anomaly magnitude rather than fixed:

```
metrics agent: min(0.95, 0.6 + 0.08 * impacted_count)
config agent:  min(0.95, 0.75 + 0.1 * max_z) when relevant changes found
               min(0.90, 0.60 + 0.1 * max_z) when anomaly detected only by count
```

## Config Agent: Incident-Relevance Priority

The config agent applies two-tier reasoning:

1. **Incident relevance first**: filter config changes whose component, change type, or parameters overlap with the incident description (keyword tokenisation + network-symptom heuristics for policy/bandwidth changes).
2. **Volume anomaly second**: run z-score detection on per-component change counts from the relevant set.

This prevents unrelated routine maintenance from being surfaced as causal.

The `incident_description` field is passed by the orchestrator in every `analyze-config` payload. Agents must forward it from the original incident object.

When `relevant_changes` is empty (the volume-anomaly-without-direct-changes branch), the finding's `start_time`/`end_time` fall back to the full `changes` list's real timestamps rather than `datetime.now()` — a synthesized `start_time == end_time == now` corrupts chronological ordering in the synthesizer's causal chain, since it no longer reflects when anything actually happened.

## Reporting Degraded Data

`TelemetryRegistry.fetch(domain, query)` returns a contract v2 `ToolResponse`. Its `coverage` list has one entry per configured endpoint, and `response.degraded` is true when any required endpoint is not `ok`. That covers a timeout, a refused connection, an open circuit breaker, a contract mismatch, or a source that only returned part of its data. Degradation is computed from **this request's** coverage. Providers are shared by every concurrent incident and hold no per-request flags.

**Every built-in agent's `analyze-<domain>` handler copies it onto the finding:**

```python
telemetry_response = await telemetry.fetch("metrics", query_from_request(data))
metrics = to_metric_snapshots(telemetry_response, data["region"])
data_degraded = telemetry_response.degraded
...
finding = AgentFinding(..., data_degraded=data_degraded)
```

Do this in any new or replacement agent too. Without it, a total telemetry outage (every finding comes back `anomaly_detected=False`) looks the same as a genuinely clean incident: both read as a confident "no anomaly" report.

`agents/rca_synthesizer.py::synthesize_report` checks `any(f.data_degraded for f in findings)`. When that is true and no finding is anomalous, it produces an explicit "RCA inconclusive: telemetry could not be retrieved for ..." report instead of the normal clean-report text. It also sets `RCAReport.data_degraded=True`, so callers can tell "verified clean" apart from "saw nothing". See [CONTRACT.md](../CONTRACT.md#no-data-vs-data-unavailable) for how "no data" differs from "data unavailable".

## Peer Collaboration Safety Rules

### Domain literal guard in fallback construction

`AgentFinding.domain` is `Literal["metrics", "log", "routing", "config"]`. If you ever construct a fallback `AgentFinding` using an untrusted sender string (e.g. from a peer message), Pydantic raises a `ValidationError` when that string is `"unknown"` or any unrecognised value.

Always guard with:

```python
valid_domains = {"metrics", "log", "routing", "config"}
safe_domain = sender if sender in valid_domains else "metrics"
peer_finding = AgentFinding(agent_id=sender, domain=safe_domain, ...)
```

## Minimal Adapter/Endpoint Skeleton

Agent endpoint should support:

- `GET /.well-known/agent.json`
- `POST /a2a` with `skill=analyze-<domain>`
- `POST /a2a` with `skill=respond-to-peer` (ack-only — see "How Peer Collaboration Actually Works")

Use `agents/a2a_protocol.py`'s `extract_request_context()`/`build_task_result()` helpers for the request/response envelope — all four built-in agents share this one module now instead of each carrying its own byte-for-byte copy. Use the built-in agents as the executable reference implementation.
