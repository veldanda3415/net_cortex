# Fine-tuning a small RCA model (experiment)

Goal: a small open model (Qwen2.5-7B-Instruct + LoRA) that writes NetCortex's RCA
synthesis at least as well as Gemini Flash Lite, compared with Nova Micro too.
Gemini and Nova are comparison baselines only.

**Hands-on steps: [LAB.md](LAB.md)** (train, evaluate, host on Bedrock, clean up). **Concepts: [CONCEPTS.md](CONCEPTS.md)** (LoRA, QLoRA, DoRA, every parameter and why).

| Script | Does |
|---|---|
| `common.py` | rebuild a drill's synthesizer prompt; judge an answer with NetCortex's guards + drill scorer |
| `label_teacher.py` | teacher samples (Gemini or Bedrock) on prompt variants, keep only verified answers |
| `build_dataset.py` | merge + dedupe + split train/val **by drill** |
| `export_sft.py` | one example per drill straight from the live RCA (no teacher) |
| `train_lora.py` | QLoRA training (answer-only loss, validation loss), merge to full weights |
| `generate.py` | greedy predictions from base or adapter (GPU box) |
| `eval_models.py` | `prompts` / `gemini` / `bedrock` / `score` on a held-out round |
| `eval_round.sh` | idempotent eval of all arms on a round |

Data: train/val from round r1 (`output/finetune/dataset/`), test = round r2
(`output/finetune/eval/prompts.jsonl`). Never train on r2.
