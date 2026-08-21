# Agent Authoring Guide

This guide explains how to build or replace a NetCortex domain agent while preserving compatibility with the orchestration engine.

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

Note on `respond-to-peer`: the registry still requires every agent to advertise this skill, and the orchestrator's collaboration loop still calls it once per peer per round (see "How Peer Collaboration Actually Works" below) — but as of this revision, an agent's own `/a2a` handler for this skill does nothing but acknowledge the message. It is **not** where cross-agent reasoning happens. Don't build new logic behind `respond-to-peer` expecting it to feed back into that round's confidence — it won't; see the next section for where the real logic lives.

## Agent Decision Pipeline

Implement this 6-step pattern in every domain:

1. Input acquisition
- Pull telemetry for region and incident window via the domain's `Provider` (`providers/factory.py` selects `simulation` or `mcp` per `config.providers.<domain>`).
- Return deterministic data shape.
- Check `getattr(provider, "degraded", False)` immediately after the call and propagate it into `AgentFinding.data_degraded` (see "Reporting Degraded Data" below) — don't let a fetch failure silently masquerade as "no anomaly."

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

This is the part most likely to mislead a new agent author, so read it before writing `reconsider_finding`.

There are two distinct things that both look like "peer collaboration" in this codebase, and only one of them does anything:

1. **`apply_local_reconsideration` (`core/orchestrator.py`, `collaboration_node`)** — the real mechanism. After each analysis round, the orchestrator calls your domain module's `reconsider_finding(finding, peer_findings)` function **directly, in-process**, passing it the peer findings it already collected in that round. This is what actually changes `confidence`, `revised`, `revision_count`, and `summary`. Implement your domain's corroboration/contradiction logic here.

2. **The `/a2a` `respond-to-peer` endpoint** — the orchestrator's `broadcast()` call *does* hit every agent's `respond-to-peer` skill once per peer per round, and it's real network traffic that gets logged to `a2a_messages.jsonl`. But as of this revision the built-in agents' handlers for it do nothing but return `{"ack": True}`. There used to be a `pending_peer_messages` queue / `session_findings` cache / `active_sessions` tracking set behind this endpoint that attempted to apply `reconsider_finding` from *within* the agent process on receipt of a live peer message — it was removed because it never actually fired in the orchestrated flow (the orchestrator's `analysis_node` already pops its own session context by the time a broadcast arrives, and the broadcast payload key didn't match what the handler read anyway). If you're replacing a built-in agent, you do **not** need to reimplement that queue — just answer `respond-to-peer` with an ack and put your real logic in `reconsider_finding`.

Practical guidance for a new/replacement agent:

- Publish finding summary + anomaly flag is handled for you by the orchestrator's `broadcast()` — you don't call this yourself.
- Reply to `respond-to-peer` with a simple ack. Don't try to do anything stateful there.
- Put all actual belief revision in `reconsider_finding(finding, peer_findings)`, called once per round by the orchestrator with that round's already-collected peer findings.
- Make `reconsider_finding` **idempotent**: guard the whole confidence/revision_count/summary mutation behind a check for whatever marker string you append to `summary` (e.g. `"Peer corroboration:" not in revised.summary`), not just the summary append itself. The orchestrator's convergence check only compares summary text between rounds — if your confidence bump isn't gated the same way as the summary marker, it will keep climbing every round even after the loop considers itself converged.

## Testing Checklist for New Agents

1. Healthy-case test returns `anomaly_detected=false`.
2. Clear anomaly test returns `anomaly_detected=true`.
3. Low-quality input lowers confidence.
4. Output schema validates as AgentFinding.
5. `respond-to-peer` returns a valid JSON-RPC ack result.
6. `reconsider_finding` (called directly, not through the endpoint) flips `revised=true` and increments `revision_count` on first call, and is a no-op on a second call with the same peer findings (idempotency).
7. A provider fetch failure (mock the provider to raise, or set `provider.degraded = True`) results in `AgentFinding.data_degraded=True`, not just an empty/no-anomaly finding.

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

Every `Provider` (`MetricsProvider`/`LogProvider`/`RoutingProvider`/`ConfigProvider`, `providers/base.py`) carries a `degraded: bool = False` class-level default. Simulation providers never set it — there's nothing to fail. `providers/adapters/mcp_adapter.py`'s MCP-backed adapters set `self.degraded = True` in their `except` branch (and reset it to `False` at the top of each call) whenever a fetch/parse fails and the method falls back to returning `[]` — this is the same "never raise" contract as before, just with a visible flag attached.

**Every built-in agent's `analyze-<domain>` handler reads this immediately after calling the provider**, before any `await`, and sets it on the constructed `AgentFinding`:

```python
metrics = provider.get_metrics(region, window_minutes, scenario_id)
data_degraded = getattr(provider, "degraded", False)
...
finding = AgentFinding(..., data_degraded=data_degraded)
```

Do this in any new/replacement agent too. Without it, a total telemetry outage (every provider call fails, every finding comes back `anomaly_detected=False`) is indistinguishable from a genuinely clean incident — both look like a confident "no anomaly" report. `agents/rca_synthesizer.py::synthesize_report` checks `any(f.data_degraded for f in findings)` and, when true and there are no anomalous findings, produces an explicit "RCA inconclusive: telemetry could not be retrieved for ..." report instead of the normal clean-report text, and sets `RCAReport.data_degraded=True` so callers can distinguish "verified clean" from "saw nothing."

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
