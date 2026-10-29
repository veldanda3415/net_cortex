#!/usr/bin/env python3
"""Score one NetCortex RCA against the faults log (the ground truth).

    score.py <fault> <truth.jsonl> <record.json> [trigger]

truth.jsonl holds the fault's events from /netcortex-lab/faults, one JSON object
per line. record.json is what the alarm bridge (or drill.sh) saved:
{"incident": <payload>, "report": <RCAReport>}. Prints one JSON object.

Verdicts:
  cause     a signal in expect.json's `cause` fired: the RCA found the culprit
  symptom   only `symptoms` fired: it saw the impact but not why
  miss      none of the expected signals fired
  clean / false_positive   for a negative control: no anomaly is correct
  stale     the RCA's window ended before the fault was injected

For an uncovered fault (no catalog rule for its cause) a cause signal can only fire
as a verified LLM hypothesis; `hypotheses` lists every one with the checks it failed,
and `no_cause_verified` scores mtu-blackhole, where blaming nothing is correct.

Rule hits are read from the key events and from each finding's full
`evidence` list, so a cause crowded out of the key events still counts. The
scoring rules live in evals/scoring.py, shared with `app/main.py eval --cases`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.scoring import headline_names_culprit, llm_used, load_expect, verdict  # noqa: E402


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(fault: str, truth_path: str, record_path: str, trigger: str = "alarm") -> dict:
    expect = load_expect()[fault]
    events = [json.loads(line) for line in Path(truth_path).read_text().splitlines() if line.strip()]
    injected = [e for e in events if e.get("fault") == fault and e.get("result") == "injected"]
    if not injected:
        raise SystemExit(f"faults log has no 'injected' event for {fault}")
    truth = injected[-1]
    reverted = next((e for e in events if e.get("result") == "reverted" and ts(e["at"]) >= ts(truth["at"])), None)

    record = json.loads(Path(record_path).read_text())
    report, incident = record["report"], record["incident"]
    findings = report.get("agent_findings", [])
    anomalous = [f for f in findings if f.get("anomaly_detected")]
    top = max(anomalous, key=lambda f: f.get("confidence", 0), default=None)

    result_verdict, cause, symptoms = verdict(expect, report)
    if ts(incident["reported_at"]) < ts(truth["at"]):
        result_verdict = "stale"
    fired = {s for f in findings for e in f.get("key_events", []) for r in e.get("rules_fired", []) for s in [r["signal"]]}
    fired |= {s for f in findings for row in f.get("evidence", []) for s in row.get("signals", [])}

    result = {
        "verdict": result_verdict,
        "trigger": trigger,
        "incident_id": report.get("incident_id"),
        "truth": {"fault": fault, "injected_at": truth["at"], "reverted_at": reverted["at"] if reverted else None},
        "rca_seconds": round((ts(report["generated_at"]) - ts(truth["at"])).total_seconds()),
        "cause_fired": cause,
        "symptoms_fired": symptoms,
        "other_fired": sorted(fired - set(cause) - set(symptoms)),
        "top_domain": top["domain"] if top else None,
        "top_cause": (lambda c: f"{c['entity_type']}:{c['entity_id']}" if c else None)((report.get("ranked_causes") or [None])[0]),
        "incident_status": report.get("incident_status"),
        "confidence": report.get("confidence_score"),
        "degraded_domains": sorted(f["domain"] for f in findings if f.get("data_degraded")),
        "root_cause": (report.get("root_cause") or "")[:300],
    }
    if expect.get("no_config_culprit"):
        result["blamed_config"] = any(f["domain"] == "config" for f in anomalous)
    result["headline_cause"] = headline_names_culprit(expect, report, cause)
    result["llm_used"] = llm_used(report)
    hyps = report.get("hypotheses") or []
    result["hypotheses"] = [
        {"signal": h.get("signal"), "entity": f"{h['entity']['type']}:{h['entity']['id']}", "verdict": h.get("verdict"),
         "failed": sorted(c for c, ok in (h.get("checks") or {}).items() if not ok)} for h in hyps]
    verified = [h for h in hyps if h.get("verdict") == "supported"]
    result["cause_by_hypothesis"] = any(h.get("signal") in expect.get("cause", []) for h in verified)
    if expect.get("no_cause_expected"):
        # Correct when nothing is blamed: no verified hypothesis and no config culprit.
        result["no_cause_verified"] = not verified and not result.get("blamed_config", False)
    return result


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        raise SystemExit(__doc__)
    print(json.dumps(main(*sys.argv[1:])))
