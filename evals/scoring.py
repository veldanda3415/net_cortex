"""How an RCA report is scored against a known fault.

Shared by infra/faults/score.py (live drills, truth from the faults log) and
`app/main.py eval --cases` (replay cases). expect.json says, per fault, which
signals point at the culprit (`cause`), which are downstream effects
(`symptoms`), or that the fault has no impact (`negative`).

A signal counts as fired when a rule fired on it anywhere in the report:
key events or the full `evidence` list (so a cause crowded out of the key
events still counts).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

EXPECT = Path(__file__).resolve().parents[1] / "infra" / "faults" / "expect.json"
DETERMINISTIC_ROOT = re.compile(r"^(\w+ domain( anomaly)?:|No anomaly detected|RCA inconclusive)")


def load_expect() -> dict[str, Any]:
    return json.loads(EXPECT.read_text())


def fired_signals(report: dict[str, Any]) -> list[str]:
    fired = set()
    for f in report.get("agent_findings", []):
        for e in f.get("key_events", []):
            fired.update(r["signal"] for r in e.get("rules_fired", []))
        for row in f.get("evidence", []):
            fired.update(row.get("signals", []))
    return sorted(fired)


def verdict(expect: dict[str, Any], report: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """(verdict, cause signals fired, symptom signals fired)."""
    fired = set(fired_signals(report))
    anomalous = [f for f in report.get("agent_findings", []) if f.get("anomaly_detected")]
    cause = sorted(fired & set(expect.get("cause", [])))
    symptoms = sorted(fired & set(expect.get("symptoms", [])))
    if expect.get("negative"):
        return ("false_positive" if anomalous else "clean"), cause, symptoms
    if cause:
        return "cause", cause, symptoms
    return ("symptom" if symptoms else "miss"), cause, symptoms


def cause_entities(expect: dict[str, Any], report: dict[str, Any]) -> set[str]:
    """Entities a cause signal fired on."""
    wanted = set(expect.get("cause", []))
    out = set()
    for f in report.get("agent_findings", []):
        for e in f.get("key_events", []):
            if {r["signal"] for r in e.get("rules_fired", [])} & wanted:
                out.add(str(e.get("entity_id", "")).lower())
        for row in f.get("evidence", []):
            if set(row.get("signals", [])) & wanted:
                out.add(str(row.get("entity_id", "")).lower())
    return out - {""}


def headline_names_culprit(expect: dict[str, Any], report: dict[str, Any], cause: list[str]) -> bool:
    """Does the root-cause sentence itself name the culprit: a cause signal,
    or the entity a cause signal fired on?"""
    root = (report.get("root_cause") or "").lower()
    return bool(cause) and any(n.lower() in root for n in [*cause, *cause_entities(expect, report)])


def llm_used(report: dict[str, Any]) -> bool:
    """The deterministic synthesizer's root causes have fixed shapes; anything else came from the LLM."""
    return not DETERMINISTIC_ROOT.match(report.get("root_cause") or "")


def score_case(case: dict[str, Any], report: dict[str, Any], expect: dict[str, Any]) -> dict[str, Any]:
    """One replay case: the drill verdict plus the cross-domain questions."""
    fault_expect = expect[case["fault"]]
    result_verdict, cause, symptoms = verdict(fault_expect, report)
    culprit = case.get("culprit")
    ranked = report.get("ranked_causes") or []
    root = (report.get("root_cause") or "").lower()
    anomalous = [f for f in report.get("agent_findings", []) if f.get("anomaly_detected")]
    messages = report.get("a2a_message_log", [])
    asked = [m for m in messages if m.get("message_type") == "validation_request"]
    supported = [m for m in asked if (m.get("payload", {}).get("response") or {}).get("verdict") == "supported"]

    out: dict[str, Any] = {
        "case": case["name"], "fault": case["fault"], "verdict": result_verdict,
        "cause_fired": cause, "symptoms_fired": symptoms,
        "status": report.get("incident_status"), "questions": len(asked), "supported": len(supported),
        "top_cause": f"{ranked[0]['entity_type']}:{ranked[0]['entity_id']} ({ranked[0]['domain']})" if ranked else None,
        "llm_used": llm_used(report),
        "root_cause": (report.get("root_cause") or "")[:240],
    }
    if culprit:
        ids = {i.lower() for i in culprit["ids"]}
        alarm_ids = {str(e.get("id", "")).lower() for e in case.get("alarm_entities", [])}
        alarm_domain = case.get("alarm_domain", "metrics")
        out["cross_domain"] = culprit["domain"] != alarm_domain or not (ids & alarm_ids)
        out["culprit_top1"] = bool(ranked) and ranked[0]["entity_type"] == culprit["type"] and ranked[0]["entity_id"].lower() in ids
        out["culprit_in_root_cause"] = any(i in root for i in ids)
    if fault_expect.get("no_config_culprit") or culprit is None:
        out["blamed_config"] = any(f.get("domain") == "config" for f in anomalous) or bool(ranked and ranked[0]["domain"] == "config")
    if case.get("expect_status"):
        out["status_ok"] = report.get("incident_status") == case["expect_status"]
    out["passed"] = _passed(case, fault_expect, out)
    return out


def _passed(case: dict[str, Any], fault_expect: dict[str, Any], out: dict[str, Any]) -> bool:
    checks = []
    if fault_expect.get("negative"):
        checks.append(out["verdict"] == "clean")
    if case.get("culprit"):
        checks += [out["verdict"] == "cause", out["culprit_top1"], out["culprit_in_root_cause"]]
    if "blamed_config" in out and not fault_expect.get("negative"):
        checks.append(not out["blamed_config"])
    if "status_ok" in out:
        checks.append(out["status_ok"])
    return all(checks) if checks else out["verdict"] in ("cause", "symptom")
