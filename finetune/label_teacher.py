"""Build verified SFT data: sample a teacher LLM on each drill's synthesizer prompt
(original + finding-order permutations), keep only answers NetCortex's guards
accept and that name the true culprit when the evidence holds it.

    python finetune/label_teacher.py output/campaign/campaign-r1.jsonl --out output/finetune/r1 \
        [--model gemini-flash-lite-latest] [--variants 4] [--samples 3] [--keep 6]

    # the teacher used for the Nova Micro fine-tune: Nova Pro (25 requests/min cross-region)
    python finetune/label_teacher.py output/campaign/campaign-r1.jsonl --out output/finetune/r1-nova-pro \
        --provider bedrock --model us.amazon.nova-pro-v1:0 --rpm 20 --variants 8 --samples 3 --keep 20

Writes <out>/train.jsonl ({"messages": [...], "meta": {...}}) and <out>/label_stats.json.
Never pass the test round here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_cases, parse_json, remap_citations  # noqa: E402


def env():
    for raw in (ROOT / "config" / ".env").read_text().splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            k, v = raw.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def gemini(model: str, temperature: float, rpm: float = 8.0):
    """A paced client: the free tier allows 15 requests/min per model and live
    NetCortex RCAs share the key, so stay well under it."""
    import threading
    import time
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=60000))
    lock, next_at = threading.Lock(), [0.0]

    def pace():
        with lock:
            wait = next_at[0] - time.monotonic()
            next_at[0] = max(next_at[0], time.monotonic()) + 60.0 / rpm
        if wait > 0:
            time.sleep(wait)

    def call(prompt: str) -> str:
        err = ""
        for attempt in range(4):
            pace()
            try:
                r = client.models.generate_content(model=model, contents=prompt, config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=temperature))
                return r.text or ""
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                if "429" in err:
                    time.sleep(30)
        return f"__error__ {err[:200]}"
    return call


def bedrock(model: str, temperature: float, rpm: float = 60.0, profile: str = "aws-veldanda-admin"):
    """Bedrock Converse teacher (e.g. Nova Micro), paced like gemini()."""
    import threading
    import time
    import boto3
    client = boto3.Session(profile_name=profile, region_name="us-east-1").client("bedrock-runtime")
    lock, next_at = threading.Lock(), [0.0]

    def call(prompt: str) -> str:
        err = ""
        for attempt in range(4):
            with lock:
                wait = next_at[0] - time.monotonic()
                next_at[0] = max(next_at[0], time.monotonic()) + 60.0 / rpm
            if wait > 0:
                time.sleep(wait)
            try:
                r = client.converse(modelId=model, messages=[{"role": "user", "content": [{"text": prompt}]}],
                                    inferenceConfig={"maxTokens": 1500, "temperature": min(temperature, 1.0)})
                return r["output"]["message"]["content"][0]["text"]
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                time.sleep(5 * (attempt + 1))
        return f"__error__ {err[:200]}"
    return call


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lines", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="gemini-flash-lite-latest")
    ap.add_argument("--variants", type=int, default=4, help="prompt variants per drill (1 = original only)")
    ap.add_argument("--samples", type=int, default=3, help="teacher samples per variant")
    ap.add_argument("--keep", type=int, default=6, help="max kept examples per drill")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rpm", type=float, default=8.0)
    ap.add_argument("--provider", choices=["gemini", "bedrock"], default="gemini")
    args = ap.parse_args()
    env()
    call = (bedrock(args.model, args.temperature, args.rpm) if args.provider == "bedrock"
            else gemini(args.model, args.temperature, args.rpm))
    cases = load_cases(args.lines)

    jobs = []  # (case, order, prompt, mapping)
    for c in cases:
        orders = [None] + c.permutations(args.variants - 1)
        for order in orders:
            prompt, mapping = c.prompt(order)
            for _ in range(args.samples):
                jobs.append((c, order, prompt, mapping))
    print(f"{len(cases)} drills, {len(jobs)} teacher calls", flush=True)
    with ThreadPoolExecutor(args.workers) as pool:
        outputs = list(pool.map(lambda j: call(j[2]), jobs))

    kept, stats = defaultdict(list), defaultdict(Counter)
    for (c, order, prompt, mapping), text in zip(jobs, outputs):
        st = stats[c.id]
        st["calls"] += 1
        ans = parse_json(text)
        if ans is None:
            st["bad_json"] += 1
            continue
        inverse = {v: k for k, v in mapping.items()}
        j = c.judge(remap_citations(ans, inverse))
        st["accepted"] += j["accepted"]
        good = j["accepted"] and (not j["culprit_case"] or j["headline_raw"])
        st["good"] += good
        if not good:
            continue
        target = {k: ans.get(k) for k in (["ranked_cause_ids"] if c.ranked else []) + [
            "root_cause", "contributing_factors", "causal_chain", "metrics_affected", "human_readable_summary"]}
        key = (prompt, target["root_cause"])
        if any(k == key for k, _ in kept[c.id]) or len(kept[c.id]) >= args.keep:
            continue
        kept[c.id].append((key, {
            "messages": [{"role": "user", "content": prompt},
                         {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)}],
            "meta": {"case": c.id, "fault": c.fault, "verdict": c.verdict, "order": order, "teacher": args.model},
        }))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = [r for v in kept.values() for _, r in v]
    with open(out / "train.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    per = {c.id: {"fault": c.fault, "verdict": c.verdict, **stats[c.id], "kept": len(kept[c.id])} for c in cases}
    summary = {"drills": len(cases), "examples": len(rows),
               "drills_with_examples": sum(1 for v in kept.values() if v),
               "teacher_accept_rate": round(sum(s["accepted"] for s in stats.values()) / max(1, len(jobs)), 3),
               "per_drill": per}
    (out / "label_stats.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_drill"}))
    for p in per.values():
        print(f"  {p['fault']:28} {p['verdict']:8} calls={p.get('calls',0)} accepted={p.get('accepted',0)} good={p.get('good',0)} kept={p['kept']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
