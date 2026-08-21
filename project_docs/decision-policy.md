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

NetCortex confidence is computed deterministically (not by LLM), in `agents/rca_synthesizer.py::compute_confidence`.

Current framework:

- `corroborating_signals / total_agents_dispatched`
- domain-weight factor based on agreeing domains
- conflict penalty when findings disagree

This ensures reproducibility across identical inputs.

**`total_agents_dispatched`, not `total_signals` that responded.** `total` in the ratio is the count of agents the supervisor actually dispatched for this incident (`len(active_agents)`), not the count of findings that came back. This matters specifically under partial failure: if 4 agents are dispatched and 3 time out or error, leaving only 1 finding, the denominator stays 4 — losing agents does not shrink the denominator and inflate the ratio. `agents/rca_synthesizer.py::synthesize_report` is called with `total_agents=len(state["active_agents"])` from the orchestrator for exactly this reason.

`RCAReport` also carries `timed_out_agents` and `errored_agents` (populated from `core/orchestrator.py`'s `analysis_node`, which now distinguishes a genuine `asyncio.TimeoutError` from any other exception during analysis) so a caller can see *why* the corroborating count is lower than the dispatched count, not just that it is.

### Worked Example

Given 4 active domains with 3 corroborating anomalies and no conflict:

- corroborating ratio = 3/4 = 0.75
- domain weight (3 agreeing domains) = 0.90
- conflict penalty = 0.00

Computed score:

`confidence = 0.75 * 0.90 * (1 - 0.00) = 0.675 -> 0.68`

### Worked Example — Partial Failure

Given 4 agents dispatched, 3 time out, and the 1 survivor is anomalous:

- corroborating ratio = 1/4 = 0.25 (denominator stays 4, the dispatched count — **not** 1/1)
- domain weight (1 agreeing domain) = 0.50
- conflict penalty = 0.00 (only one finding exists, so the unanimity check below doesn't trigger)

Computed score:

`confidence = 0.25 * 0.50 * (1 - 0.00) = 0.125 -> 0.12`

Losing 3 of 4 agents to timeout does not make the report *more* confident — it stays low, and `timed_out_agents` on the report says why.

**Known remaining simplification (intentionally out of scope of the fix above):** `conflict_detected` is `0 < corroborating < len(findings)` — it's computed against the number of findings that actually *came back*, not the dispatched count, and it means "not unanimous among survivors," not "genuinely contradictory." A single-domain finding that's the only survivor (as in the example above) reads as non-conflicting for this reason, even though 3 domains never got to weigh in. This is a known, named simplification, not an oversight — see `docs/NetCortex_Code_Review.md` §6 for the fuller critique if you're evaluating whether to redesign it.

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

### Verified-Clean vs. Degraded-Data "No Anomaly"

A report of "no anomaly" carries two structurally different meanings that must not be collapsed into the same text: telemetry was retrieved and genuinely showed nothing wrong, versus telemetry could not be retrieved at all. The never-raise provider adapters (`providers/adapters/mcp_adapter.py`) degrade every failure — timeout, unreachable server, malformed response — to an empty result at WARNING, which is correct behavior for *that* layer (an agent shouldn't crash because telemetry is unavailable), but it means an agent's own finding can't distinguish "verified clean" from "saw nothing" without an explicit signal.

That signal is `AgentFinding.data_degraded` (set by each agent from `provider.degraded` immediately after the fetch call) and `RCAReport.data_degraded` (true if any finding is degraded). When every finding is non-anomalous *and* at least one is degraded, `agents/rca_synthesizer.py::synthesize_report` produces `root_cause = "RCA inconclusive: telemetry could not be retrieved for <domains>, so 'no anomaly' cannot be verified."` instead of the normal `"No anomaly detected across all monitored domains."` text — a total outage must never read as a confident clean report.

## Governance Recommendations

For production adoption, define policy thresholds:

- Auto-action threshold (for example confidence >= 0.85 and no conflict)
- Human-review threshold (for example 0.5 to 0.85)
- No-action threshold (for example < 0.5)

These thresholds should be tuned with offline replay against historical incidents.
