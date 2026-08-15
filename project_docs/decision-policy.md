# Decision Policy

This document defines how NetCortex transforms multiple domain findings into a single RCA conclusion.

## Goal

Produce one conclusion that is:

- Evidence-grounded
- Reproducible
- Explainable
- Robust to partial disagreement

## Decision Stages

1. Supervisor scoping
- Parse incident description.
- Select active agents.
- Pass `incident_description` string in every domain analysis payload so config agent can apply incident-relevance filtering.

2. Parallel domain analysis
- Each agent emits AgentFinding independently.
- Metrics and config agents use per-entity z-score baselines from `BaselineProvider` before falling back to hard-coded thresholds.

3. Collaboration rounds
- Agents share findings and peer feedback.
- Agents revise findings once per round.

4. Synthesis
- Deterministic confidence aggregation in code.
- LLM narrative generation from structured evidence.

## Evidence Alignment Rules

A finding is considered corroborating when at least two of the following match:

- Time overlap with other anomalous findings
- Shared affected component/path/service
- Shared impact pattern (for example throughput drop plus error spike)

Contradiction is flagged when findings make mutually incompatible cause claims.

## Root Cause Selection Policy

1. Filter to anomalous findings.
2. Rank by confidence.
3. Apply corroboration boost for findings validated by other domains.
4. Select top-ranked cause as primary root cause.
5. Record remaining high-signal findings as contributing factors.

If no anomalous findings remain, conclude no-anomaly outcome.

## Confidence Policy

NetCortex confidence is computed deterministically (not by LLM).

Current framework:

- `corroborating_signals / total_signals`
- domain-weight factor based on agreeing domains
- conflict penalty when findings disagree

This ensures reproducibility across identical inputs.

### Worked Example

Given 4 active domains with 3 corroborating anomalies and no conflict:

- corroborating ratio = 3/4 = 0.75
- domain weight (3 agreeing domains) = 0.90
- conflict penalty = 0.00

Computed score:

`confidence = 0.75 * 0.90 * (1 - 0.00) = 0.675 -> 0.68`

## Conflict Handling Policy

When conflicts exist:

1. Keep conflicting findings in report.
2. Apply confidence penalty.
3. Set `conflict_detected=true`.
4. Preserve contradictory evidence in `agent_findings` and `a2a_message_log`.

Do not hide disagreement for the sake of a cleaner narrative.

Simulation coverage note:

- Scenario `10` in `simulation/scenarios.py` intentionally creates conflicting evidence: metrics detects high error rate/throughput degradation on Switch-C while config reports no proximate changes.
- Use `python app/main.py eval --all-scenarios` to verify this policy path remains exercised over time.

## LLM Policy

LLM is used for narrative synthesis only.

LLM must not:

- Invent evidence not present in findings
- Override deterministic confidence score
- Suppress conflict signals

If LLM is unavailable and strict mode is off:
- Use deterministic fallback narrative.

If strict mode is on:
- Fail run when LLM output cannot be produced.

## No-Conclusion and Low-Confidence Policy

When confidence is low or data is weak:

- Report best hypothesis with explicit uncertainty.
- Include top missing evidence categories.
- Suggest what telemetry would disambiguate cause.

This is preferable to overconfident incorrect root cause claims.

## Governance Recommendations

For production adoption, define policy thresholds:

- Auto-action threshold (for example confidence >= 0.85 and no conflict)
- Human-review threshold (for example 0.5 to 0.85)
- No-action threshold (for example < 0.5)

These thresholds should be tuned with offline replay against historical incidents.
