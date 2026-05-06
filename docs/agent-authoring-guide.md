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

If any of these are missing, registration fails.

## Agent Decision Pipeline

Implement this 6-step pattern in every domain:

1. Input acquisition
- Pull telemetry for region and incident window.
- Return deterministic data shape.

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
- `start_time`, `end_time`: analysis window used by this finding.
- `confidence`: numeric confidence from deterministic formula.
- `revised` and `revision_count`: collaboration-phase updates.

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
	"revision_count": 0
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

## Peer Collaboration Behavior

During collaboration rounds:

- Publish finding summary with anomaly flag.
- Reply to peer clarification requests with bounded, domain-only evidence.
- Do not revise immediately on every message.
- Revise once per round after collecting all peer responses.

## Testing Checklist for New Agents

1. Healthy-case test returns `anomaly_detected=false`.
2. Clear anomaly test returns `anomaly_detected=true`.
3. Low-quality input lowers confidence.
4. Output schema validates as AgentFinding.
5. Peer response path returns valid JSON-RPC result.
6. Round revision flips `revised=true` and increments `revision_count`.

## Minimal Adapter/Endpoint Skeleton

Agent endpoint should support:

- `GET /.well-known/agent.json`
- `POST /a2a` with `skill=analyze-<domain>`
- `POST /a2a` with `skill=respond-to-peer`

Use the built-in agents as the executable reference implementation.
