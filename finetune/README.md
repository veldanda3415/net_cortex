# Fine-tuning a small RCA model (experiment)

Goal: does fine-tuning make a small model (Nova Micro) better at NetCortex's RCA synthesis
than the same model with few-shot prompting? Teacher: Nova Pro; verifier: NetCortex's own guards.
Gemini Flash Lite is a reference baseline only. Qwen (QLoRA on a GPU box) is deferred.

**Hands-on steps: [LAB.md](LAB.md)** (label with Nova Pro, tune Nova Micro on Bedrock, evaluate, clean up).
Deferred QLoRA path: [LAB-qwen.md](LAB-qwen.md). Concepts: [CONCEPTS.md](CONCEPTS.md).

| Script | Does |
|---|---|
| `common.py` | rebuild a drill's synthesizer prompt; judge an answer with NetCortex's guards + drill scorer |
| `label_teacher.py` | teacher samples (Gemini or Bedrock) on prompt variants, keep only verified answers |
| `build_dataset.py` | merge + dedupe + split train/val **by drill** |
| `export_sft.py` | one example per drill straight from the live RCA (no teacher) |
| `to_nova.py` | convert to Bedrock's conversation format for Nova fine-tuning, check Nova's rules |
| `train_lora.py` | (deferred) QLoRA training (answer-only loss, validation loss), merge to full weights |
| `generate.py` | (deferred) greedy predictions from base or adapter (GPU box) |
| `eval_models.py` | `prompts` / `gemini` / `bedrock` / `score` on a held-out round |
| `eval_round.sh` | idempotent eval of all arms on a round |

Data: train/val from round r1 (`output/finetune/dataset/`), test = round r2
(`output/finetune/eval/prompts.jsonl`). Never train on r2.
