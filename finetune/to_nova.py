"""Convert our chat JSONL to the Bedrock conversation format for Nova fine-tuning.

    python finetune/to_nova.py output/finetune/dataset --out output/finetune/nova --limit 300

Reads <dataset>/train.jsonl and val.jsonl (from build_dataset.py, already split by drill),
takes an equal share per drill up to --limit (train) and --val-limit, and writes
<out>/train.jsonl and <out>/val.jsonl as:

  {"schemaVersion": "bedrock-conversation-2024",
   "messages": [{"role": "user", "content": [{"text": PROMPT}]},
                {"role": "assistant", "content": [{"text": ANSWER}]}]}

No system turn: NetCortex sends the whole instruction in the user turn, and the prompt
must be identical at training and inference time.

Checks Nova's rules: 8..20,000 records, 32k-token context, and the reserved strings
(User:, Bot:, Assistant:, System:, <image>, <video>, [EOS]) that fail a job.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

RESERVED = re.compile(r"User:|Bot:|Assistant:|System:|<image>|<video>|\[EOS\]")
CHARS_PER_TOKEN = 3.6       # measured on this data with the Qwen tokenizer; Nova's is similar
PRICE_PER_M_TOKENS = 1.25   # rough, from AWS's Nova Micro example ($2.18 for 1.75M); check the pricing page


def balanced(rows: list[dict], limit: int, rng: random.Random) -> list[dict]:
    by_drill = defaultdict(list)
    for r in rows:
        by_drill[r["meta"]["case"]].append(r)
    for v in by_drill.values():
        rng.shuffle(v)
    out, i = [], 0
    while len(out) < limit and any(i < len(v) for v in by_drill.values()):
        for v in by_drill.values():  # round-robin: every drill gets the same share
            if i < len(v) and len(out) < limit:
                out.append(v[i])
        i += 1
    rng.shuffle(out)
    return out


def convert(r: dict) -> dict:
    user, answer = r["messages"][0]["content"], r["messages"][1]["content"]
    for text in (user, answer):
        m = RESERVED.search(text)
        if m:
            raise SystemExit(f"reserved string {m.group(0)!r} in {r['meta']['case']}: replace it before training")
    return {"schemaVersion": "bedrock-conversation-2024",
            "messages": [{"role": "user", "content": [{"text": user}]},
                         {"role": "assistant", "content": [{"text": answer}]}]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--val-limit", type=int, default=40)
    ap.add_argument("--epochs", type=int, default=2, help="only for the cost estimate")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    total_tokens = 0
    for name, limit in (("train", a.limit), ("val", a.val_limit)):
        rows = [json.loads(x) for x in (Path(a.dataset) / f"{name}.jsonl").read_text().splitlines() if x.strip()]
        picked = balanced(rows, limit, rng)
        if name == "train" and not 8 <= len(picked) <= 20000:
            raise SystemExit(f"Nova needs 8..20000 training records, got {len(picked)}")
        longest = 0
        with open(out / f"{name}.jsonl", "w") as fh:
            for r in picked:
                rec = convert(r)
                n = len(rec["messages"][0]["content"][0]["text"]) + len(rec["messages"][1]["content"][0]["text"])
                longest = max(longest, n)
                if name == "train":
                    total_tokens += n / CHARS_PER_TOKEN
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        drills = len({r["meta"]["case"] for r in picked})
        print(f"{name}: {len(picked)} records from {drills} drills, longest ~{longest / CHARS_PER_TOKEN:,.0f} tokens (limit 32k)")
    est = total_tokens * a.epochs
    print(f"training tokens: ~{total_tokens / 1e6:.2f}M per epoch, ~{est / 1e6:.2f}M for {a.epochs} epochs, "
          f"roughly ${est / 1e6 * PRICE_PER_M_TOKENS:.2f} (estimate; check Bedrock pricing) + model storage per month")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
