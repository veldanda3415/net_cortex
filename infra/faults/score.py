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

Only key events are inspected (up to 3-5 per domain), so a signal that fired
but was crowded out of the key events counts as not fired.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

EXPECT = Path(__file__).resolve().parent / "expect.json"
DETERMINISTIC_ROOT = re.compile(r"^(\w+ domain( anomaly)?:|No anomaly detected|RCA inconclusive)")


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(fault: str, truth_path: str, record_path: str, trigger: str = "alarm") -> dict:
    expect = json.loads(EXPECT.read_text())[fault]
    events = [json.loads(line) for line in Path(truth_path).read_text().splitlines() if line.strip()]
    injected = [e for e in events if e.get("fault") == fault and e.get("result") == "injected"]
    if not injected:
        raise SystemExit(f"faults log has no 'injected' event for {fault}")
    truth = injected[-1]
    reverted = next((e for e in events if e.get("result") == "reverted" and ts(e["at"]) >= ts(truth["at"])), None)

    record = json.loads(Path(record_path).read_text())
    report, incident = record["report"], record["incident"]
    findings = report.get("agent_findings", [])
    fired = sorted({r["signal"] for f in findings for e in f.get("key_events", []) for r in e.get("rules_fired", [])})
    anomalous = [f for f in findings if f.get("anomaly_detected")]
    top = max(anomalous, key=lambda f: f.get("confidence", 0), default=None)

    cause = sorted(set(fired) & set(expect.get("cause", [])))
    symptoms = sorted(set(fired) & set(expect.get("symptoms", [])))
    if ts(incident["reported_at"]) < ts(truth["at"]):
        verdict = "stale"
    elif expect.get("negative"):
        verdict = "false_positive" if anomalous else "clean"
    elif cause:
        verdict = "cause"
    elif symptoms:
        verdict = "symptom"
    else:
        verdict = "miss"

    result = {
        "verdict": verdict,
        "trigger": trigger,
        "incident_id": report.get("incident_id"),
        "truth": {"fault": fault, "injected_at": truth["at"], "reverted_at": reverted["at"] if reverted else None},
        "rca_seconds": round((ts(report["generated_at"]) - ts(truth["at"])).total_seconds()),
        "cause_fired": cause,
        "symptoms_fired": symptoms,
        "other_fired": sorted(set(fired) - set(cause) - set(symptoms)),
        "top_domain": top["domain"] if top else None,
        "confidence": report.get("confidence_score"),
        "degraded_domains": sorted(f["domain"] for f in findings if f.get("data_degraded")),
        "root_cause": (report.get("root_cause") or "")[:300],
    }
    if expect.get("no_config_culprit"):
        result["blamed_config"] = any(f["domain"] == "config" for f in anomalous)

    # Does the root-cause sentence itself name the culprit: a cause signal, or the
    # entity a cause signal fired on? (The evidence can hold the cause while the
    # headline still leads with a symptom.)
    root = result["root_cause"].lower()
    cause_entities = {
        str(e.get("entity_id", "")).lower()
        for f in findings for e in f.get("key_events", [])
        if {r["signal"] for r in e.get("rules_fired", [])} & set(expect.get("cause", []))
    } - {""}
    result["headline_cause"] = bool(cause) and any(n.lower() in root for n in [*cause, *cause_entities])
    # The deterministic synthesizer's root causes have fixed shapes; anything else came from the LLM.
    result["llm_used"] = not DETERMINISTIC_ROOT.match(report.get("root_cause") or "")
    return result


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        raise SystemExit(__doc__)
    print(json.dumps(main(*sys.argv[1:])))
