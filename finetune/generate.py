"""Greedy predictions from a local model (GPU box) on eval prompts.

    python finetune/generate.py --prompts prompts.jsonl --out pred-qwen-base.jsonl
    python finetune/generate.py --prompts prompts.jsonl --adapter /opt/ft/qwen7b-r1/adapter --out pred-qwen-tuned.jsonl
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="Qwen/Qwen2.5-7B-Instruct")
    ap.add_argument("--adapter")
    ap.add_argument("--max-new-tokens", type=int, default=1200)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(a.base)
    model = AutoModelForCausalLM.from_pretrained(a.base, dtype=torch.bfloat16, device_map="cuda")
    if a.adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, a.adapter)
    model.eval()
    rows = [json.loads(x) for x in Path(a.prompts).read_text().splitlines() if x.strip()]
    with open(a.out, "w") as fh:
        for i, r in enumerate(rows, 1):
            t = time.time()
            text_in = tok.apply_chat_template([{"role": "user", "content": r["prompt"]}], add_generation_prompt=True,
                                              tokenize=False)
            ids = tok(text_in, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
            with torch.no_grad():
                out = model.generate(ids, max_new_tokens=a.max_new_tokens, do_sample=False,
                                     pad_token_id=tok.pad_token_id or tok.eos_token_id)
            text = tok.decode(out[0, ids.shape[1]:], skip_special_tokens=True)
            fh.write(json.dumps({"case": r["case"], "output": text}) + "\n")
            fh.flush()
            print(f"[{i}/{len(rows)}] {r['fault']} in={ids.shape[1]} out={out.shape[1] - ids.shape[1]} {time.time() - t:.0f}s",
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
