"""Merge verified teacher samples and split them into train / validation BY DRILL.

    python finetune/build_dataset.py output/finetune/r1*/train.jsonl --out output/finetune/dataset \
        [--val-frac 0.15] [--cap 80] [--seed 7]

Why by drill: every sample of one incident (finding orders, teacher answers) shares
the same evidence. Splitting samples at random would put near-copies of a validation
incident in training, and validation loss would look better than it is. The test
set is a different campaign round (r2), never passed here.

Writes <out>/train.jsonl, <out>/val.jsonl and <out>/split.json (which drill went where).
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--cap", type=int, default=80, help="max samples per drill (keeps one drill from dominating)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--holdout-faults", type=float, default=0.0,
                    help="leave this fraction of fault TYPES out of train and val entirely (generalization test)")
    a = ap.parse_args()
    rng = random.Random(a.seed)

    by_drill, seen = defaultdict(list), set()
    for path in a.inputs:
        for raw in Path(path).read_text().splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            key = (row["messages"][0]["content"], row["messages"][1]["content"])
            if key in seen:
                continue
            seen.add(key)
            by_drill[row["meta"]["case"]].append(row)

    drills = sorted(by_drill)
    if any(d.split(":")[0] != "r1" for d in drills):
        raise SystemExit("only training-round drills belong here; the test round must stay unseen")
    heldout = set()
    if a.holdout_faults:
        # stratified by verdict: hold out half of the cause faults, half of the symptom faults, ...
        fault_verdict = {by_drill[d][0]["meta"]["fault"]: by_drill[d][0]["meta"]["verdict"] for d in drills}
        groups = defaultdict(list)
        for f, v in sorted(fault_verdict.items()):
            groups[v].append(f)
        for v, fs in sorted(groups.items()):
            k = round(a.holdout_faults * len(fs))
            heldout |= set(rng.sample(fs, k))
        drills = [d for d in drills if by_drill[d][0]["meta"]["fault"] not in heldout]
        print(f"held-out fault types ({len(heldout)}): {', '.join(sorted(heldout))}")
    # stratify the validation pick by verdict so it isn't all one kind of incident
    by_verdict = defaultdict(list)
    for d in drills:
        by_verdict[by_drill[d][0]["meta"]["verdict"]].append(d)
    n_val = max(1, round(a.val_frac * len(drills)))
    val = []
    pools = [rng.sample(v, len(v)) for _, v in sorted(by_verdict.items(), key=lambda kv: -len(kv[1]))]
    while len(val) < n_val and any(pools):
        for p in pools:
            if p and len(val) < n_val and len(p) > 1:  # never take a verdict's only drill
                val.append(p.pop())
    val = set(val)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    split = {"train": {}, "val": {}, "heldout_faults": sorted(heldout)}
    for name in ("train", "val"):
        rows = []
        for d in drills:
            if (d in val) != (name == "val"):
                continue
            picked = rng.sample(by_drill[d], min(a.cap, len(by_drill[d])))
            rows += picked
            split[name][d] = {"samples": len(picked), "verdict": picked[0]["meta"]["verdict"],
                              "teachers": dict(Counter(r["meta"]["teacher"] for r in picked))}
        rng.shuffle(rows)
        with open(out / f"{name}.jsonl", "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{name}: {len(rows)} samples from {len(split[name])} drills")
    (out / "split.json").write_text(json.dumps(split, indent=2))
    print("val drills:", ", ".join(sorted(split["val"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
