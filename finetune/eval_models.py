"""Evaluate RCA synthesis models on a held-out drill round.

    # 1. prompts for the test round (unpermuted, exactly what NetCortex sends)
    python finetune/eval_models.py prompts output/campaign/campaign-r2.jsonl --out output/finetune/eval/prompts.jsonl
    # 2. predictions: Gemini here; Qwen base/tuned on the GPU box (finetune/generate.py)
    python finetune/eval_models.py gemini output/finetune/eval/prompts.jsonl --out output/finetune/eval/pred-gemini.jsonl
    # 3. score every predictions file against the same round
    python finetune/eval_models.py score output/campaign/campaign-r2.jsonl output/finetune/eval/pred-*.jsonl

Prediction files are JSON lines {"case": id, "output": raw text}. Metrics, from
NetCortex's own guards and evals/scoring.py:
  json_ok        parsable JSON with a root_cause
  accepted       NetCortex would keep the model's root cause (citations resolve and it names the top cause)
  headline_raw   the model's root cause names the true culprit (drills whose evidence holds it)
  headline_final what NetCortex finally reports, after falling back on rejected answers
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_cases, parse_json  # noqa: E402


def cmd_prompts(a):
    cases = load_cases(a.lines)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w") as fh:
        for c in cases:
            fh.write(json.dumps({"case": c.id, "fault": c.fault, "prompt": c.prompt()[0]}) + "\n")
    print(f"{len(cases)} prompts -> {a.out}")


def cmd_gemini(a):
    import label_teacher as L
    L.env()
    call = L.gemini(a.model, a.temperature, a.rpm)
    rows = [json.loads(x) for x in Path(a.prompts).read_text().splitlines() if x.strip()]
    with ThreadPoolExecutor(2) as pool:
        outs = list(pool.map(lambda r: call(r["prompt"]), rows))
    with open(a.out, "w") as fh:
        for r, o in zip(rows, outs):
            fh.write(json.dumps({"case": r["case"], "output": o}) + "\n")
    print(f"{len(rows)} predictions -> {a.out}")


def cmd_bedrock(a):
    """Bedrock Converse (e.g. Nova Micro) on the same prompts, admin profile, paced."""
    import time
    import boto3
    client = boto3.Session(profile_name=a.profile, region_name="us-east-1").client("bedrock-runtime")
    rows = [json.loads(x) for x in Path(a.prompts).read_text().splitlines() if x.strip()]
    shots = []
    if a.fewshot:
        # one verified example per verdict kind (cause, symptom, clean, ...), shortest prompt first,
        # taken from the TRAINING split only, as earlier conversation turns
        pool = [json.loads(x) for x in Path(a.fewshot_from).read_text().splitlines() if x.strip()]
        pool.sort(key=lambda r: len(r["messages"][0]["content"]))
        picked = []
        for kind in ["cause", "symptom", "clean", "miss"][: a.fewshot]:
            r = next((r for r in pool if r["meta"]["verdict"] == kind), None)
            if r:
                picked.append(r)
        for r in picked:
            shots += [{"role": "user", "content": [{"text": r["messages"][0]["content"]}]},
                      {"role": "assistant", "content": [{"text": r["messages"][1]["content"]}]}]
        print(f"few-shot: {', '.join(r['meta']['case'] for r in picked)}")
    with open(a.out, "w") as fh:
        for r in rows:
            text = ""
            for attempt in range(4):
                try:
                    resp = client.converse(modelId=a.model, messages=shots + [{"role": "user", "content": [{"text": r["prompt"]}]}],
                                           inferenceConfig={"maxTokens": 1500})
                    text = resp["output"]["message"]["content"][0]["text"]
                    break
                except Exception as exc:  # noqa: BLE001
                    text = f"__error__ {str(exc)[:200]}"
                    time.sleep(10 * (attempt + 1))
            fh.write(json.dumps({"case": r["case"], "output": text}) + "\n")
            fh.flush()
            time.sleep(60.0 / a.rpm)
    print(f"{len(rows)} predictions -> {a.out}")


def cmd_score(a):
    cases = {c.id: c for c in load_cases([a.lines])}
    table = []
    detail = {}
    for path in a.preds:
        preds = {}
        for x in Path(path).read_text().splitlines():
            if x.strip():
                r = json.loads(x)
                preds.setdefault(r["case"], []).append(r["output"])
        name = Path(path).stem.removeprefix("pred-")
        rows = []
        for cid, c in cases.items():
            for out in preds.get(cid, []):
                j = c.judge(parse_json(out) if out else None)
                try:  # what NetCortex itself does with the reply: json.loads, no fence stripping
                    strict = isinstance(json.loads(out), (dict, list))
                except Exception:
                    strict = False
                rows.append({"case": cid, "fault": c.fault, "verdict": c.verdict, "strict_json": strict, **j})
        groups = [("all", rows)]
        if a.split:  # seen = fault types in the training drills; validation and held-out types are unseen
            sp = json.loads(Path(a.split).read_text())
            seen = {d.split(":")[1] for d in sp.get("train", {})}
            groups += [("seen faults", [r for r in rows if r["fault"] in seen]),
                       ("unseen faults", [r for r in rows if r["fault"] not in seen])]
        for gname, grows in groups:
            culprit = [r for r in grows if r["culprit_case"]]
            frac = lambda xs, k: f"{sum(r[k] for r in xs)}/{len(xs)}"  # noqa: E731
            table.append({"model": name, "subset": gname, "n": len(grows), "strict_json": frac(grows, "strict_json"),
                          "json_ok": frac(grows, "json_ok"), "accepted": frac(grows, "accepted"),
                          "headline_raw": frac(culprit, "headline_raw"), "headline_final": frac(culprit, "headline_final"),
                          "missing": sum(1 for c in cases if c not in preds) if gname == "all" else ""})
        detail[name] = rows
    keys = list(table[0])
    print("counts are passed/total; headline_* only over drills whose evidence holds the culprit")
    print(" | ".join(keys))
    for t in table:
        print(" | ".join(str(t[k]) for k in keys))
    if a.failures:
        print("\nfailures (not accepted, or culprit not named):")
        for name, rows in detail.items():
            for r in rows:
                why = [k for k in ("strict_json", "cited_ok", "names_top") if not r[k]]
                if r["culprit_case"] and not r["headline_raw"]:
                    why.append("culprit_not_named")
                if why:
                    print(f"  {name:22} {r['fault']:28} {', '.join(why)}")
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": table, "detail": detail}, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompts"); p.add_argument("lines", nargs="+"); p.add_argument("--out", required=True)  # noqa: E702
    g = sub.add_parser("gemini"); g.add_argument("prompts"); g.add_argument("--out", required=True)  # noqa: E702
    g.add_argument("--model", default="gemini-flash-lite-latest")
    g.add_argument("--temperature", type=float, default=None)
    g.add_argument("--rpm", type=float, default=8.0)
    b = sub.add_parser("bedrock"); b.add_argument("prompts"); b.add_argument("--out", required=True)  # noqa: E702
    b.add_argument("--model", default="us.amazon.nova-micro-v1:0")
    b.add_argument("--profile", default="aws-veldanda-admin")
    b.add_argument("--rpm", type=float, default=20.0)
    b.add_argument("--fewshot", type=int, default=0, help="number of verified training examples to show first")
    b.add_argument("--fewshot-from", default="output/finetune/dataset/train.jsonl")
    s = sub.add_parser("score"); s.add_argument("lines"); s.add_argument("preds", nargs="+")  # noqa: E702
    s.add_argument("--out")
    s.add_argument("--split", help="split.json of a leave-fault-out dataset: report seen vs unseen fault types")
    s.add_argument("--failures", action="store_true", help="list every failed prediction and why")
    a = ap.parse_args()
    {"prompts": cmd_prompts, "gemini": cmd_gemini, "bedrock": cmd_bedrock, "score": cmd_score}[a.cmd](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
