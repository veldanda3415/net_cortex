"""Teacher labeling as a Bedrock BATCH job (about half the on-demand price; minutes to hours).

    # 1. build the job input: every drill x N finding orders, Converse request format
    python finetune/batch.py make output/campaign/campaign-r1.jsonl --out output/finetune/batch --variants 6
    # 2. upload + create the job (LAB.md part 9), wait, download the output .jsonl.out files
    # 3. verify and keep, exactly like label_teacher.py
    python finetune/batch.py collect output/campaign/campaign-r1.jsonl output/finetune/batch \
        --outputs output/finetune/batch/out/*.jsonl.out --model us.amazon.nova-micro-v1:0

Bedrock needs at least 100 records per job; `make` refuses fewer.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_cases, parse_json, remap_citations  # noqa: E402

KEYS = ["root_cause", "contributing_factors", "causal_chain", "metrics_affected", "human_readable_summary"]


def cmd_make(a):
    cases = load_cases([a.lines])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    index = {}
    n = 0
    with open(out / "input.jsonl", "w") as fh:
        for ci, c in enumerate(cases):
            for vi, order in enumerate([None] + c.permutations(a.variants - 1)):
                prompt, mapping = c.prompt(order)
                rid = f"c{ci:03d}v{vi:02d}"
                index[rid] = {"case": c.id, "order": order, "mapping": mapping}
                fh.write(json.dumps({"recordId": rid, "modelInput": {
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "inferenceConfig": {"maxTokens": 1500, "temperature": a.temperature}}}) + "\n")
                n += 1
    if n < 100:
        raise SystemExit(f"{n} records: Bedrock batch jobs need at least 100 (raise --variants)")
    (out / "index.json").write_text(json.dumps(index))
    print(f"{n} records from {len(cases)} drills -> {out / 'input.jsonl'} (+ index.json to map results back)")


def answer_text(rec: dict) -> str | None:
    mo = rec.get("modelOutput") or {}
    try:  # Converse response shape
        return mo["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


def cmd_collect(a):
    cases = {c.id: c for c in load_cases([a.lines])}
    index = json.loads((Path(a.batch_dir) / "index.json").read_text())
    kept, stats = defaultdict(list), Counter()
    for path in a.outputs:
        for raw in Path(path).read_text().splitlines():
            if not raw.strip():
                continue
            rec = json.loads(raw)
            stats["records"] += 1
            meta = index.get(rec.get("recordId"))
            text = answer_text(rec)
            if meta is None or text is None:
                stats["errors"] += 1
                continue
            c = cases[meta["case"]]
            ans = parse_json(text)
            if ans is None:
                stats["bad_json"] += 1
                continue
            inverse = {v: k for k, v in meta["mapping"].items()}
            j = c.judge(remap_citations(ans, inverse))
            stats["accepted"] += j["accepted"]
            if not (j["accepted"] and (not j["culprit_case"] or j["headline_raw"])):
                continue
            stats["good"] += 1
            target = {k: ans.get(k) for k in (["ranked_cause_ids"] if c.ranked else []) + KEYS}
            prompt = c.prompt(meta["order"])[0]
            kept[c.id].append({"messages": [{"role": "user", "content": prompt},
                                            {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)}],
                               "meta": {"case": c.id, "fault": c.fault, "verdict": c.verdict,
                                        "order": meta["order"], "teacher": f"{a.model} (batch)"}})
    out = Path(a.batch_dir) / "train.jsonl"
    with open(out, "w") as fh:
        for rows in kept.values():
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps({**stats, "drills_with_examples": len(kept)}), "->", out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make"); m.add_argument("lines"); m.add_argument("--out", required=True)  # noqa: E702
    m.add_argument("--variants", type=int, default=6)
    m.add_argument("--temperature", type=float, default=0.8)
    c = sub.add_parser("collect"); c.add_argument("lines"); c.add_argument("batch_dir")  # noqa: E702
    c.add_argument("--outputs", nargs="+", required=True)
    c.add_argument("--model", default="us.amazon.nova-micro-v1:0")
    a = ap.parse_args()
    {"make": cmd_make, "collect": cmd_collect}[a.cmd](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
