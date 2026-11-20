# Concepts: LoRA, QLoRA, DoRA, and why each knob is set the way it is

Read this before LAB.md part 4. Every number here is from *this* model and *this* data, so you
can check it on your own screen.

## 1. The problem

Qwen2.5-7B has **7.6 billion** weights. Full fine-tuning updates all of them: the weights (bf16, 2
bytes each, 15 GB), their gradients (15 GB) and Adam's two moment estimates (fp32, 61 GB) is more
than 90 GB before a single activation. One 24-48 GB GPU can't do it, and ~1,000 examples from ~20
incidents could never justify moving 7.6B numbers anyway.

## 2. LoRA: learn a small *change*, not new weights

For a weight matrix `W` (d x k), freeze it and learn a low-rank update:

```
W' = W + (alpha / r) · B · A        A: r x k,  B: d x r,  r << d, k
```

- `B` starts at **zero**, so at step 0 the model is exactly the base model. Training only moves
  it as far as the data pushes.
- **Rank `r`** is the capacity of the change: the update can only move the matrix in `r`
  directions. The bet (which holds well for "learn a format/behaviour" tasks like ours) is that
  the change needed is low-rank even though `W` isn't.
- **`alpha / r`** scales the update. Keeping `alpha = 2r` keeps the update's size roughly constant
  when you change `r`, so you can change rank without re-tuning the learning rate.
- **Parameter count:** `r · (d + k)` per matrix. Qwen2.5-7B has 28 layers, hidden size 3584, MLP
  size 18944, and grouped-query attention (k/v project to 512). At r=16 on all 7 projections:

  | matrix | d + k | r·(d+k) |
  |---|---|---|
  | q_proj, o_proj | 3584 + 3584 | 114,688 each |
  | k_proj, v_proj | 3584 + 512 | 65,536 each |
  | gate, up, down | 3584 + 18944 | 360,448 each |
  | **per layer** | | **1,441,792** |
  | **x 28 layers** | | **40,370,176** = the `trainable params` line the script prints (0.53%) |

- **After training** you can keep the adapter separate (165 MB, swap it per task) or **merge**
  it: compute `W + (alpha/r)·B·A` once and save a normal model (15 GB). Merged = no inference
  overhead and any server (Bedrock, vLLM) can load it. That's why Bedrock gets the merged copy.

## 3. QLoRA: same LoRA, but the frozen base is stored in 4 bits

The frozen `W` doesn't need full precision because it is never updated:

- **NF4 (4-bit NormalFloat):** 16 levels placed where normally-distributed weights actually lie,
  so 4 bits lose very little. 7.6B weights: ~15 GB in bf16 becomes ~5 GB.
- **Double quantization:** the per-block scale factors are quantized too (saves ~0.4 GB more).
- **Compute in bf16:** each 4-bit block is de-quantized on the fly for the matrix multiply;
  gradients flow through it into the LoRA matrices, which stay in full precision.
- Cost: ~20-30% slower than bf16 LoRA (the de-quantizing), and a small quality loss that
  usually doesn't matter for a task like this.

In `train_lora.py`: `--method qlora` (default) = `BitsAndBytesConfig(load_in_4bit, nf4,
double_quant, compute bf16)` + `prepare_model_for_kbit_training`.

**Memory budget of our run** (why it fits a 24 GB L4): 4-bit base ~5 GB + LoRA weights and Adam
states ~0.5 GB (40M x ~12 bytes) + **activations for a 12k-token sequence** (the big one, cut down
by gradient checkpointing: recompute instead of store) + logits. Logits were the trap: 12k tokens
x 152k vocabulary x 4 bytes = ~7 GB. We only grade the answer, so `AnswerOnlyTrainer` computes
logits for the ~600 answer tokens only (`logits_to_keep`).

## 4. DoRA: split each weight into *how big* and *which direction*

DoRA (Weight-Decomposed LoRA) rewrites each weight column as `magnitude x direction`:

```
W = m · V / ||V||       m: one number per output column,  V/||V||: unit direction
W' = (m + Δm) · (V + B·A) / ||V + B·A||
```

LoRA updates the direction; the magnitude vector `m` is trained directly (it's tiny). Studies of
full fine-tuning show it changes magnitude and direction somewhat independently, which plain LoRA
can't do. DoRA can, so it tends to get closer to full fine-tuning, especially at **low rank**.

- Cost: computing the column norms every step makes training slower (often ~1.2-2x) and uses
  some more memory. It merges back into a normal weight just like LoRA.
- **QDoRA** = DoRA on a 4-bit base. `--method dora` / `--method qdora` (`use_dora=True` in PEFT).

## 5. Our knobs and why

| knob | value | why |
|---|---|---|
| base | Qwen2.5-7B-Instruct | already follows instructions and writes JSON; Bedrock can import it (and serve it via Converse); Apache-2.0 |
| method | QLoRA | fits a 24 GB GPU; quality loss small for a format/behaviour task |
| rank r | 16 | small data (~20 incidents) needs little capacity; bigger r memorises faster |
| alpha | 32 (= 2r) | keeps update scale stable across ranks |
| dropout | 0.05 | light regularisation on the adapter input |
| targets | all 7 projections | attention-only learns *where to look*; MLP layers carry *what to write* (format, phrasing); both help here |
| learning rate | 2e-4, cosine, 2 warmup steps | standard LoRA range (much higher than full fine-tuning's ~1e-5, because B starts at zero and only 0.5% of weights move) |
| batch | 1 x grad-accum 8 | sequences are 3-12k tokens: one at a time, averaged over 8 for a stable step |
| epochs | 1 | the ~1,000 samples are variants of ~20 incidents: a second pass mostly memorises |
| max length | 12288 | longest prompt ~11.7k tokens; longer ones lose the middle of the prompt, never the answer |
| loss | answer tokens only | we want it to learn to *write* the RCA, not to predict the prompt |
| precision | bf16 compute | same range as fp32, half the memory; L4/L40S support it |

## 6. Hands-on experiments (LAB.md part 4b)

Same data, change one knob, compare **validation loss** and the **test scorer**. Use a subset to
keep each run short and cheap (`--limit 300 --no-merge`, roughly 20-40 min and ~$1 each on a
g6e.xlarge):

| run | flags | what you learn |
|---|---|---|
| A baseline | `--method qlora --rank 16` | reference |
| B low rank | `--rank 4` | is 16 more than needed? |
| C high rank | `--rank 64` | does more capacity help, or just overfit faster (watch eval_loss)? |
| D DoRA | `--method qdora --rank 16` | quality vs speed (compare samples/s in the log) |
| E bf16 LoRA | `--method lora` (needs the 48 GB L40S) | what 4-bit quantization costs, if anything |
| F attention only | `--targets attn` | where the behaviour lives |

Pick 3-4; write a one-line result for each (val loss at the end, `accepted%`, `headline_raw%`,
time). That table is your interview story.

## 7. "Verify behaviour" means more than one number

1. **Format contract:** plain `json.loads` works, all keys present (`strict_json%`).
2. **NetCortex's guards:** citations exist, root cause names the top-ranked cause (`accepted%`).
3. **Correctness on unseen incidents:** names the real culprit on r2 (`headline_raw%`).
4. **Honesty on no-cause faults:** on `mtu-blackhole` / `mss-clamp-removed` it should not invent a
   config change (read those answers yourself).
5. **Robustness:** same drill, findings in a different order: same root cause?
6. **Hosting parity:** the Bedrock-hosted model scores like the GPU-box model (LAB part 7d).
7. **Cost and latency:** seconds per RCA and $ per 1,000 RCAs vs Gemini/Nova.

## 8. Questions to be able to answer

- Why does LoRA start from exactly the base model? What would break if `B` started random?
- What does rank control, and what does `alpha/r` buy you?
- Where does the memory go in QLoRA training, and which item was our out-of-memory error?
- Why is QLoRA slower than LoRA, and why was it still the right call on a 24 GB GPU?
- What does DoRA change, and when is it worth the slower training?
- Why split train/validation **by incident**, and why test on a different **day's** drills?
- Your training loss falls and validation loss rises: what's happening and what do you change?
- Adapter vs merged weights: when do you ship which?
- How do you know the hosted model behaves like the one you evaluated?
