"""False-alarm probe: does a model invent a culprit when the evidence holds none?

Built offline from the test round (r2), never from training data. For each scored
"cause" drill, only the evidence on entities the alarm itself names is kept (the
alarmed flow, tunnel or peer dipping below its threshold); everything else, including
other symptoms that point at the real fault, is removed, the agents left with no
evidence turn clean, summaries are rebuilt from what's left (the originals name the
culprit), and the rules' ranked causes are dropped.
NetCortex only calls the LLM when some agent still reports an anomaly, so these are
the cases the LLM really sees. Three kinds:

  transient     symptoms only, incident status "recovered" (a blip that cleared)
  no-cause      symptoms only, status "ongoing", nothing ranked (cause not visible)
  red-herring   transient + one harmless config change: a real Route 53 change event
                rewritten to a TXT upsert on an unrelated name (domain verification),
                placed 25 min before the symptom

The correct answer in all three: no specific root cause. Scoring is a text check on
root_cause (see judge()); failures are printed for a human read.

    python finetune/false_alarms.py make output/campaign/campaign-r2.jsonl --out output/finetune/false_alarms/prompts.jsonl
    python finetune/eval_models.py bedrock output/finetune/false_alarms/prompts.jsonl --out .../pred-<arm>.jsonl ...
    python finetune/false_alarms.py score output/finetune/false_alarms/prompts.jsonl output/finetune/false_alarms/pred-*.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CATALOG, load_cases, parse_json  # noqa: E402

from agents.rca_synthesizer import _build_llm_prompt, _evidence_index  # noqa: E402

KINDS = ("transient", "no-cause", "red-herring")
# --abstain-prompt: the one-sentence change to NetCortex's root_cause instruction that this
# probe tests (an experiment variant here, not a change to agents/rca_synthesizer.py)
ROOT_LINE = ("  root_cause: one concise sentence naming the specific root cause (the top candidate's entity id and "
             "what changed, magnitude if known), with [E<n>] citations\n")
ROOT_LINE_ABSTAIN = ROOT_LINE.rstrip("\n") + (
    ". If no candidate cause is ranked and no cited evidence shows a change that explains the alarm, say that no "
    "root cause was identified, describe the symptom, and do not blame a change the evidence doesn't link to it; "
    "if the incident status is recovered, say the degradation was transient and has recovered\n")
ABSTAIN = re.compile(
    r"no (?:clear |definitive |specific |single |identifiable |confirmed )?(?:root )?cause|"
    r"(?:not|could not|cannot|can't) (?:be )?(?:identified|determined|established|confirmed|pinpointed)|"
    r"undetermined|unable to (?:identify|determine)|insufficient|inconclusive|unclear|unknown|"
    r"no (?:evidence|candidate)|without (?:a )?(?:clear |identified )?cause|false (?:alarm|positive)", re.I)
TRANSIENT = re.compile(r"recover|transient|cleared|resolved|brief|flap|self-heal", re.I)


def _signals(row: dict) -> set[str]:
    return set(row.get("signals") or []) | {r.get("signal") for r in row.get("rules_fired") or [] if r.get("signal")}


def strip_to_alarm(case) -> list:
    desc = case.incident.get("description", "")
    cause = set(case.expect.get("cause") or [])

    def keep(r):
        return str(r.get("entity_id")) in desc and not (_signals(r) & cause)
    out = []
    for f in case.findings:
        g = f.model_copy(deep=True)
        g.evidence = [r for r in g.evidence if keep(r)]
        g.key_events = [e for e in g.key_events if keep(e)]
        g.unclassified_evidence = []
        if not g.evidence:
            g.anomaly_detected, g.key_events, g.confidence = False, [], 0.1
            g.summary = "No anomalies detected in the query window."
        else:  # the original summary names the real culprit ("worst tgw=..."): rebuild it from what's left
            reasons = sorted({f"{r.get('entity_id')}: " + "; ".join(x.get("reason", "") for x in r.get("rules_fired", [])[:1])
                              for r in g.evidence})
            g.summary = f"Anomalies detected on {len(reasons)} entities; evidence=" + " | ".join(reasons)
        out.append(g)
    return out


HERRING_REQUEST = json.dumps({"changeBatch": {"changes": [{"action": "UPSERT", "resourceRecordSet": {
    "name": "_verify.lab.internal.", "type": "TXT", "ttl": 300,
    "resourceRecords": [{"value": "\"site-verification=4f2a9c\""}]}}]}, "hostedZoneId": "{zone}"})


def herring_row(cases, case, before):
    """A real ChangeResourceRecordSets row whose request is rewritten to a harmless TXT
    upsert on an unrelated name (a domain-verification record), moved to 25 minutes
    before the first symptom so timing alone doesn't rule it out."""
    from datetime import timedelta
    for other in cases:
        for f in other.findings:
            for row in f.evidence if f.domain == "config" else []:
                if "ChangeResourceRecordSets" in (row.get("signals") or []):
                    at = (before - timedelta(minutes=25)).isoformat()
                    row = {**row, "first_seen": at, "last_seen": at}
                    ev = next((e for e in f.key_events if str(e.get("entity_id")) == str(row.get("entity_id"))), None)
                    if ev:
                        ev = {**ev, "timestamp": at, "username": "ops-automation",
                              "request": HERRING_REQUEST.replace("{zone}", str(row.get("entity_id")))}
                    return row, ev
    return None, None


def cmd_make(a):
    cases = [c for c in load_cases(a.lines) if c.verdict == "cause"]
    rows = []
    for c in cases:
        base = strip_to_alarm(c)
        if not any(f.anomaly_detected for f in base):
            continue  # nothing left: NetCortex would not call the LLM at all
        for kind in KINDS:
            findings = [f.model_copy(deep=True) for f in base]
            status = "ongoing" if kind == "no-cause" else "recovered"
            planted = None
            if kind == "red-herring":
                from datetime import datetime
                firsts = [r.get("first_seen") for f in base for r in f.evidence if r.get("first_seen")]
                row, ev = herring_row(cases, c, datetime.fromisoformat(str(min(firsts)).replace("Z", "+00:00")))
                if row is None:
                    continue
                cfg = next((f for f in findings if f.domain == "config"), None)
                if cfg is None:
                    continue
                cfg.evidence = [row] + cfg.evidence
                cfg.key_events = ([ev] if ev else []) + cfg.key_events
                cfg.anomaly_detected, cfg.confidence = True, max(cfg.confidence, 0.6)
                cfg.summary = f"Config change found: {', '.join(row.get('signals') or [])} on {row.get('entity_id')}."
                planted = str(row.get("entity_id"))
            evidence = _evidence_index(findings)
            prompt = _build_llm_prompt(c.incident.get("description", ""), findings, [], CATALOG, [], evidence, status)
            if a.abstain_prompt:
                assert ROOT_LINE in prompt, "the synthesizer's root_cause instruction changed: update ROOT_LINE"
                prompt = prompt.replace(ROOT_LINE, ROOT_LINE_ABSTAIN)
            rows.append({"case": f"fa{'x' if a.abstain_prompt else ''}:{kind}:{c.fault}", "fault": c.fault, "kind": kind,
                         "planted": planted, "prompt": prompt})
        if len({r["fault"] for r in rows}) >= a.drills:
            break
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    by = defaultdict(int)
    for r in rows:
        by[r["kind"]] += 1
    print(f"{len(rows)} prompts from {len({r['fault'] for r in rows})} drills: {dict(by)} -> {a.out}")


def judge(row: dict, text: str) -> dict:
    ans = parse_json(text)
    root = ans.get("root_cause") if isinstance(ans, dict) and isinstance(ans.get("root_cause"), str) else ""
    abstains = bool(root and ABSTAIN.search(root))
    herring = re.compile(r"ChangeResourceRecordSets|\bDNS\b|Route ?53|hosted.zone|" + re.escape(row.get("planted") or "@@"), re.I)
    blames_planted = bool(row.get("planted") and herring.search(root) and not abstains)
    return {"json_ok": bool(root), "abstains": abstains, "notes_transient": bool(TRANSIENT.search(root)),
            "blames_planted": blames_planted, "root_cause": root}


def cmd_score(a):
    prompts = {json.loads(x)["case"]: json.loads(x) for x in Path(a.prompts).read_text().splitlines() if x.strip()}
    summary = []
    for path in a.preds:
        model = Path(path).stem.removeprefix("pred-")
        per = defaultdict(lambda: defaultdict(int))
        fails = []
        for raw in Path(path).read_text().splitlines():
            if not raw.strip():
                continue
            p = json.loads(raw)
            row = prompts.get(p["case"])
            if row is None or str(p.get("output", "")).startswith("__error__"):
                continue
            j = judge(row, p["output"])
            for k in ("all", row["kind"]):
                per[k]["n"] += 1
                per[k]["json_ok"] += j["json_ok"]
                per[k]["abstains"] += j["abstains"]
                per[k]["notes_transient"] += j["notes_transient"]
                per[k]["blames_planted"] += j["blames_planted"]
            if not j["abstains"]:
                fails.append((row["kind"], row["fault"], j["root_cause"][:220]))
        for k in ("all",) + KINDS:
            if per[k]["n"]:
                s = per[k]
                summary.append({"model": model, "kind": k, "n": s["n"], "abstains": f"{s['abstains']}/{s['n']}",
                                "notes_transient": f"{s['notes_transient']}/{s['n']}",
                                "blames_planted": f"{s['blames_planted']}/{s['n']}" if k in ("all", "red-herring") else ""})
        if a.failures:
            print(f"\n## {model}: {len(fails)} answers that name a culprit")
            for kind, fault, root in fails:
                print(f"  [{kind}] {fault}: {root}")
    print("\nmodel | kind | n | abstains | notes_transient | blames_planted")
    for s in summary:
        print(" | ".join(str(s[k]) for k in ("model", "kind", "n", "abstains", "notes_transient", "blames_planted")))
    if a.out:
        Path(a.out).write_text(json.dumps(summary, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make"); m.add_argument("lines", nargs="+"); m.add_argument("--out", required=True)  # noqa: E702
    m.add_argument("--drills", type=int, default=5)
    m.add_argument("--abstain-prompt", action="store_true", help="allow 'no root cause identified' in the instruction")
    s = sub.add_parser("score"); s.add_argument("prompts"); s.add_argument("preds", nargs="+")  # noqa: E702
    s.add_argument("--out"); s.add_argument("--failures", action="store_true")  # noqa: E702
    a = ap.parse_args()
    {"make": cmd_make, "score": cmd_score}[a.cmd](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
