"""Export scored drill RCAs as chat-format SFT examples for a small model.

Each example is (user: the exact synthesizer prompt Gemini got, assistant: the
report's final JSON). The prompt is rebuilt with agents.rca_synthesizer's own
_build_llm_prompt from what the saved report holds, so a fine-tuned model sees
the same input Gemini did and the comparison is fair.

Inputs: campaign/drill JSON lines (infra/faults/drill.sh output, one per drill,
with `record` and `netcortex`). Ground truth comes only from those lines' score
(score.py ran against the faults log); this script never reads the faults log.

    python finetune/export_sft.py output/campaign/campaign-r1.jsonl [more.jsonl ...] \
        --out output/finetune/r1

Writes <out>/all.jsonl (every scored RCA, with meta) and <out>/train.jsonl
(only examples whose RCA was right: verdict cause, culprit named in the
headline, and the LLM actually wrote it), plus <out>/summary.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NETCORTEX_CATALOG_DIR", str(ROOT / "config" / "catalog.aws-lab"))

from agents.rca_synthesizer import _build_llm_prompt, _evidence_index  # noqa: E402
from contract.catalog_loader import get_catalog  # noqa: E402
from models.schemas import A2AMessage, AgentFinding, RankedCause  # noqa: E402

TARGET_KEYS = ["root_cause", "contributing_factors", "causal_chain", "metrics_affected", "human_readable_summary"]
CITE = re.compile(r"\[(E\d+)\]")


def _cid(c: dict) -> int:
    m = re.fullmatch(r"C(\d+)", str(c.get("id", "")))
    return int(m.group(1)) if m else 10**9


def build_example(line: dict, catalog) -> dict:
    rec_path = Path(line["record"])
    rec = json.loads(rec_path.read_text())
    report, incident = rec["report"], rec["incident"]
    findings = [AgentFinding.model_validate(f) for f in report.get("agent_findings", [])]
    messages = [A2AMessage.model_validate(m) for m in report.get("a2a_message_log", [])]
    final_ranked = report.get("ranked_causes") or []
    # ids C1..Cn are assigned in the rules order before the LLM re-ranks; that is the prompt's order
    prompt_ranked = [RankedCause.model_validate(c) for c in sorted(final_ranked, key=_cid)]
    evidence = _evidence_index(findings)
    prompt = _build_llm_prompt(incident.get("description", ""), findings, messages, catalog,
                               prompt_ranked, evidence, report.get("incident_status", "unknown"))

    target = {}
    if final_ranked:
        target["ranked_cause_ids"] = [c["id"] for c in final_ranked]
    for k in TARGET_KEYS:
        target[k] = report.get(k)

    nc = line.get("netcortex") or {}
    cited = CITE.findall(report.get("root_cause") or "")
    meta = {
        "fault": line.get("fault"),
        "round": line.get("round"),
        "incident_id": report.get("incident_id"),
        "record": str(rec_path),
        "trigger": nc.get("trigger"),
        "verdict": nc.get("verdict"),
        "headline_cause": nc.get("headline_cause"),
        "llm_used": nc.get("llm_used"),
        "top_cause": nc.get("top_cause"),
        # sanity check on the rebuild: the report's citations must exist in the rebuilt evidence
        "citations_resolve": all(c in evidence for c in cited),
        "prompt_chars": len(prompt),
    }
    return {
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)},
        ],
        "meta": meta,
    }


def good_label(meta: dict) -> bool:
    return (meta["verdict"] == "cause" and meta["headline_cause"] is True
            and meta["llm_used"] is True and meta["citations_resolve"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lines", nargs="+", help="campaign/drill JSON-lines files")
    ap.add_argument("--out", required=True, help="output directory")
    args = ap.parse_args()

    catalog = get_catalog()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_rows, skipped = [], Counter()
    for path in args.lines:
        for raw in Path(path).read_text().splitlines():
            if not raw.strip():
                continue
            line = json.loads(raw)
            if not line.get("record"):
                skipped[line.get("result") or "no-record"] += 1
                continue
            try:
                all_rows.append(build_example(line, catalog))
            except Exception as exc:  # one bad record shouldn't lose the night's data
                skipped[f"error: {type(exc).__name__}"] += 1
                print(f"skip {line.get('fault')}: {exc}", file=sys.stderr)

    train = [r for r in all_rows if good_label(r["meta"])]
    for name, rows in (("all.jsonl", all_rows), ("train.jsonl", train)):
        with open(out / name, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = {
        "examples": len(all_rows),
        "train": len(train),
        "verdicts": Counter(r["meta"]["verdict"] for r in all_rows),
        "citations_unresolved": sum(not r["meta"]["citations_resolve"] for r in all_rows),
        "max_prompt_chars": max((r["meta"]["prompt_chars"] for r in all_rows), default=0),
        "skipped": skipped,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=dict))
    print(json.dumps(summary, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
