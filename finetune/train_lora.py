"""QLoRA fine-tune of a small open model on export_sft.py output, then merge.

    python finetune/train_lora.py --data output/finetune/r1/train.jsonl \
        --out /opt/ft/qwen7b-r1 [--base Qwen/Qwen2.5-7B-Instruct] [--epochs 3]

Writes <out>/adapter (LoRA weights) and <out>/merged (full bf16 safetensors +
tokenizer: the form Bedrock Custom Model Import takes). Sized for one 24 GB GPU
(g6.2xlarge L4 / g5.2xlarge A10G): 4-bit base, batch 1, gradient checkpointing.
Only the assistant turn (the RCA JSON) is trained on; the prompt is masked.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, DataCollatorForSeq2Seq,
                          Trainer, TrainingArguments)


class AnswerOnlyTrainer(Trainer):
    """Compute logits only for the answer span: full-sequence logits for a 12k-token
    prompt over a 152k vocab are ~7 GB and don't fit next to a 7B model on 24 GB.
    Batch size 1, no padding: the answer is the tail of the sequence."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # our loss is already a per-token mean: let the Trainer average it over gradient
        # accumulation, so logged train loss is on the same scale as validation loss
        self.model_accepts_loss_kwargs = False

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        labels = inputs.pop("labels")
        length = labels.shape[1]
        first = int((labels != -100).int().argmax(dim=1).min())
        keep = length - first + 1
        out = model(input_ids=inputs["input_ids"], attention_mask=inputs["attention_mask"], logits_to_keep=keep)
        logits = out.logits[:, :-1, :].float()
        target = labels[:, length - keep + 1:]
        loss = torch.nn.functional.cross_entropy(logits.reshape(-1, logits.size(-1)), target.reshape(-1),
                                                 ignore_index=-100)
        return (loss, out) if return_outputs else loss


def load_rows(path: str) -> list[dict]:
    return [json.loads(x)["messages"] for x in Path(path).read_text().splitlines() if x.strip()]


def tokenize(messages: list[dict], tok, max_len: int) -> dict:
    # render to text, then tokenize: apply_chat_template(tokenize=True) changed return type across versions
    prompt_text = tok.apply_chat_template(messages[:-1], add_generation_prompt=True, tokenize=False)
    full_text = tok.apply_chat_template(messages, tokenize=False)
    assert full_text.startswith(prompt_text), "chat template: prompt is not a prefix of the full conversation"
    prompt_ids = tok(prompt_text, add_special_tokens=False)["input_ids"]
    full_ids = prompt_ids + tok(full_text[len(prompt_text):], add_special_tokens=False)["input_ids"]
    labels = [-100] * len(prompt_ids) + full_ids[len(prompt_ids):]
    if len(full_ids) > max_len:
        # drop from the prompt's middle, never the answer
        keep_tail = len(full_ids) - len(prompt_ids)
        if keep_tail >= max_len:
            raise ValueError(f"answer alone is {keep_tail} tokens > max_len {max_len}")
        cut = len(full_ids) - max_len
        mid = len(prompt_ids) // 2
        full_ids = full_ids[:mid] + full_ids[mid + cut:]
        labels = labels[:mid] + labels[mid + cut:]
    return {"input_ids": full_ids, "attention_mask": [1] * len(full_ids), "labels": labels}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="Qwen/Qwen2.5-7B-Instruct")
    ap.add_argument("--epochs", type=float, default=3)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--max-len", type=int, default=8192)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--val", help="validation jsonl (drills held out by build_dataset.py)")
    ap.add_argument("--eval-steps", type=int, default=20, help="optimizer steps between validation-loss checks")
    # --- knobs for the hands-on experiments (CONCEPTS.md) ---
    ap.add_argument("--method", choices=["qlora", "lora", "dora", "qdora"], default="qlora",
                    help="qlora: 4-bit frozen base + LoRA; lora: bf16 base + LoRA (needs ~2x GPU memory); "
                         "dora/qdora: weight-decomposed LoRA on a bf16 / 4-bit base")
    ap.add_argument("--alpha", type=float, help="LoRA alpha (default 2 x rank); the update is scaled by alpha/rank")
    ap.add_argument("--dropout", type=float, default=0.05)
    ap.add_argument("--targets", choices=["all", "attn"], default="all",
                    help="all: attention + MLP projections; attn: q/k/v/o only")
    ap.add_argument("--grad-accum", type=int, default=8, help="effective batch size (batch is 1)")
    ap.add_argument("--limit", type=int, help="train on the first N examples only (quick experiments)")
    ap.add_argument("--no-merge", action="store_true", help="skip writing the 15 GB merged model")
    args = ap.parse_args()
    out = Path(args.out)

    tok = AutoTokenizer.from_pretrained(args.base)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    rows = load_rows(args.data)[: args.limit]
    ds = Dataset.from_list([tokenize(m, tok, args.max_len) for m in rows])
    print(f"{len(ds)} examples, max tokens {max(len(x) for x in ds['input_ids'])}")
    val_ds = Dataset.from_list([tokenize(m, tok, args.max_len) for m in load_rows(args.val)]) if args.val else None
    if val_ds is not None:
        print(f"{len(val_ds)} validation examples")

    four_bit = args.method in ("qlora", "qdora")
    model = AutoModelForCausalLM.from_pretrained(
        args.base,
        quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                               bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
        if four_bit else None,
        dtype=torch.bfloat16, device_map="auto")
    if four_bit:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    else:
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()
    targets = ["q_proj", "k_proj", "v_proj", "o_proj"] + (["gate_proj", "up_proj", "down_proj"] if args.targets == "all" else [])
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=args.alpha or 2 * args.rank, lora_dropout=args.dropout, task_type="CAUSAL_LM",
        target_modules=targets, use_dora=args.method in ("dora", "qdora")))
    print(f"method={args.method} rank={args.rank} alpha={args.alpha or 2 * args.rank} targets={args.targets} "
          f"lr={args.lr} grad_accum={args.grad_accum}")
    model.print_trainable_parameters()

    AnswerOnlyTrainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(out / "ckpt"), num_train_epochs=args.epochs, learning_rate=args.lr,
            per_device_train_batch_size=1, gradient_accumulation_steps=args.grad_accum, warmup_steps=2,
            lr_scheduler_type="cosine", logging_steps=1, save_strategy="epoch", save_total_limit=1,
            bf16=True, gradient_checkpointing=True, report_to=[],
            eval_strategy="steps" if val_ds is not None else "no", eval_steps=args.eval_steps,
            per_device_eval_batch_size=1, prediction_loss_only=True),
        train_dataset=ds,
        eval_dataset=val_ds,
        data_collator=DataCollatorForSeq2Seq(tok, padding=True, label_pad_token_id=-100),
    ).train()
    model.save_pretrained(out / "adapter")
    tok.save_pretrained(out / "adapter")
    if args.no_merge:
        print(f"adapter: {out / 'adapter'} (merge skipped)")
        return 0

    # Merge into a full-precision copy of the base for Bedrock Custom Model Import / vLLM.
    del model
    torch.cuda.empty_cache()
    base = AutoModelForCausalLM.from_pretrained(args.base, dtype=torch.bfloat16, device_map="cpu")
    merged = PeftModel.from_pretrained(base, out / "adapter").merge_and_unload()
    merged.save_pretrained(out / "merged", safe_serialization=True)
    tok.save_pretrained(out / "merged")
    print(f"adapter: {out / 'adapter'}\nmerged:  {out / 'merged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
