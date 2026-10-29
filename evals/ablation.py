"""Catalog ablation: can NetCortex still find a cause its catalog doesn't know?

    python -m evals.ablation --runs 3 [--cases evals/cases] [--mode rules|llm|both] [--out results.jsonl]

For every replay case with a culprit, the catalog loses the signal that names
the cause (through a $NETCORTEX_CATALOG_DIR override; an entry set to null
deletes it), and any source-assigned name that gives the answer away is
renamed to a neutral one in a copy of the fixture (bgp_open_error ->
frr_unmatched, as the live passthrough would name the line). Raw names a
source cannot help using (a CloudWatch metric or a CloudTrail eventName) stay.
The controls (healthy, loss-latency) run with the full catalog and must stay
free of blame.

Each case runs N times rules-only (LLM env empty) and N times with the LLM,
each in its own process (the catalog is per process). Scored per run:

  culprit_top1     the culprit entity ranks first
  named            the root cause names it
  cause_verified   a verified hypothesis on the culprit entity, on the ablated signal
  blamed           controls: an anomaly, a verified hypothesis, or a config culprit
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.scoring import load_expect, score_case  # noqa: E402

DEFAULT_SIGNALS = json.loads((ROOT / "contract" / "catalog" / "signals.json").read_text())["signals"]

# case -> what the catalog loses. `names`: raw names dropped from an entry's
# names/match_names; `delete`: whole signals; `rename`: fixture signal renames.
ABLATIONS: dict[str, dict[str, Any]] = {
    "asn-mismatch": {"names": {"frr_log": ["bgp_open_error"]}, "rename": {"bgp_open_error": "frr_unmatched"},
                     "ablated": "frr_unmatched"},
    "bad-route-map": {"delete": ["PrefixesSent"], "ablated": "PrefixesSent"},
    "bgp-self-recovered": {"delete": ["BgpPeerState"], "ablated": "BgpPeerState"},
    "peering-route-removed": {"names": {"cloudtrail_network_mutation": ["DeleteRoute"]}, "ablated": "DeleteRoute"},
    "phz-record-deleted": {"names": {"cloudtrail_network_mutation": ["ChangeResourceRecordSets"]},
                           "ablated": "ChangeResourceRecordSets"},
    "sg-rule-removed": {"names": {"cloudtrail_network_mutation": ["ModifySecurityGroupRules"]},
                        "ablated": "ModifySecurityGroupRules"},
    "tgw-propagation-removed": {"names": {"cloudtrail_network_mutation": ["DisableTransitGatewayRouteTablePropagation"]},
                                "ablated": "DisableTransitGatewayRouteTablePropagation"},
    "tunnel-flapping": {"delete": ["TunnelState"], "ablated": "TunnelState"},
}
CONTROLS = ("healthy", "loss-latency")


def catalog_override(spec: dict[str, Any], out: Path) -> Path:
    """signals.json + investigation.json overrides that remove the cause."""
    out.mkdir(parents=True, exist_ok=True)
    signals: dict[str, Any] = {name: None for name in spec.get("delete", [])}
    dropped: list[str] = list(spec.get("delete", []))
    for entry, names in spec.get("names", {}).items():
        sig = copy.deepcopy(DEFAULT_SIGNALS[entry])
        sig["match_names"] = [n for n in sig.get("match_names", []) if n not in names]
        if (sig.get("rule") or {}).get("names"):
            sig["rule"]["names"] = [n for n in sig["rule"]["names"] if n not in names]
        signals[entry] = sig
        dropped += names
    (out / "signals.json").write_text(json.dumps({"signals": signals}, indent=1))
    # The cause roles go too: a name the catalog doesn't know has no role.
    investigation = {"signal_roles": {n: None for n in dropped}, "role_with": {n: None for n in dropped}}
    (out / "investigation.json").write_text(json.dumps(investigation, indent=1))
    return out


def ablated_case(case: dict[str, Any], spec: dict[str, Any], out: Path) -> dict[str, Any]:
    case = copy.deepcopy(case)
    rename = spec.get("rename") or {}
    if rename:
        fixture = json.loads((ROOT / case["fixture"]).read_text())
        for s in fixture["signals"]:
            if s["name"] in rename:
                s["name"] = rename[s["name"]]
        path = out / Path(case["fixture"]).name
        path.write_text(json.dumps(fixture))
        case["fixture"] = str(path)
    return case


def score_run(case: dict[str, Any], report: dict[str, Any], spec: dict[str, Any] | None) -> dict[str, Any]:
    base = score_case(case, report, load_expect())
    verified = [h for h in report.get("hypotheses", []) if h.get("verdict") == "supported"]
    row = {"case": case["name"], "top_cause": base["top_cause"], "root_cause": base["root_cause"],
           "hypotheses": len(report.get("hypotheses", [])), "verified": len(verified),
           "checks_failed": sorted({c for h in report.get("hypotheses", []) for c, ok in (h.get("checks") or {}).items()
                                    if not ok})}
    culprit = case.get("culprit")
    if culprit and spec:
        ids = {i.lower() for i in culprit["ids"]}
        row["culprit_top1"] = base["culprit_top1"]
        row["named"] = base["culprit_in_root_cause"]
        row["cause_verified"] = any(h["signal"] == spec["ablated"] and str(h["entity"]["id"]).lower() in ids
                                    for h in verified) or any(
            r.get("via") == "hypothesis" and spec["ablated"] in r.get("signals", []) and r["entity_id"].lower() in ids
            for f in report.get("agent_findings", []) for r in f.get("evidence", []))
    else:
        anomalous = any(f.get("anomaly_detected") for f in report.get("agent_findings", []))
        fault_expect = load_expect()[case["fault"]]
        row["blamed"] = bool(verified) or bool(base.get("blamed_config")) or (fault_expect.get("negative") and anomalous)
        row["passed"] = base["passed"] and not verified
    return row


def run_once(case_path: Path, catalog_dir: Path | None, llm: bool) -> dict[str, Any]:
    env = dict(os.environ, PYTHONHASHSEED="0")
    if catalog_dir is not None:
        env["NETCORTEX_CATALOG_DIR"] = str(catalog_dir)
    else:
        env.pop("NETCORTEX_CATALOG_DIR", None)
    if not llm:
        env["GEMINI_API_KEY"] = ""
        env["GOOGLE_CLOUD_PROJECT"] = ""
    proc = subprocess.run([sys.executable, "app/main.py", "eval", "--cases", str(case_path.parent)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=900)
    found = re.findall(r"output: (output/[^/]+/rca_report\.json)", proc.stdout)
    if not found:
        raise RuntimeError(f"eval produced no report: {proc.stdout[-500:]} {proc.stderr[-1500:]}")
    return json.loads((ROOT / found[-1]).read_text()) | {"_report_path": found[-1]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.ablation")
    parser.add_argument("--cases", default="evals/cases")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--mode", choices=["rules", "llm", "both"], default="both")
    parser.add_argument("--only", nargs="*", help="case names")
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)
    modes = ["rules", "llm"] if args.mode == "both" else [args.mode]
    out = sys.stdout if args.out == "-" else open(args.out, "a")
    work = Path(tempfile.mkdtemp(prefix="ablation-"))
    try:
        for path in sorted((ROOT / args.cases).glob("*.json")):
            case = json.loads(path.read_text())
            name = case["name"]
            if args.only and name not in args.only:
                continue
            spec = ABLATIONS.get(name)
            if spec is None and name not in CONTROLS:
                continue
            here = work / name
            (here / "case").mkdir(parents=True)
            catalog_dir = catalog_override(spec, here / "catalog") if spec else None
            ablated = ablated_case(case, spec, here) if spec else case
            (here / "case" / f"{name}.json").write_text(json.dumps(ablated))
            for mode in modes:
                for i in range(args.runs):
                    try:
                        report = run_once(here / "case" / f"{name}.json", catalog_dir, llm=(mode == "llm"))
                    except Exception as exc:  # one failed run is a result, not the end of the eval
                        print(json.dumps({"mode": mode, "run": i + 1, "case": name, "error": str(exc)[-300:]}),
                              file=out, flush=True)
                        continue
                    row = {"mode": mode, "run": i + 1, **score_run(ablated, report, spec),
                           "report": report["_report_path"]}
                    print(json.dumps(row), file=out, flush=True)
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
