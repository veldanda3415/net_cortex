# Hands-on lab: does fine-tuning help a small model write NetCortex's RCA?

**The question:** take one small model, Nova Micro. Is it better at NetCortex's RCA synthesis
after fine-tuning than with few-shot prompting? Everything else stays fixed: same model family,
same prompts, same test round (r2), same scorer.

| Arm | What it is | Why it's here |
|---|---|---|
| `nova-micro` | base model, zero-shot | floor |
| `nova-micro-fewshot` | base model, 3 verified examples in the prompt | the cheap alternative to tuning |
| `nova-pro` | the teacher, zero-shot | ceiling: a student rarely beats its teacher |
| `nova-micro-tuned` | Nova Micro, managed fine-tune on **Nova Pro** labels | **the experiment** |
| `nova-micro-tuned-self` (optional) | Nova Micro tuned on its **own** verified labels | does the teacher matter, or only the verifier? |
| `gemini-flash-lite` | reference, already run | different family, context only |

Qwen (QLoRA on your own GPU box) is deferred: [LAB-qwen.md](LAB-qwen.md). Comparing tuned Qwen
with Nova would mix two questions, "does tuning help?" and "own model vs managed?".

Region us-east-1, profile `aws-veldanda-admin`. Run everything from the repo root in WSL:

```bash
export AWS_PROFILE=aws-veldanda-admin AWS_REGION=us-east-1 AWS_PAGER=
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=netcortex-ft-$ACCT
```

| Part | What you learn | Time | Cost |
|---|---|---|---|
| 1. Look at the data | what one example is; why the split is by drill | 10 min | $0 |
| 2. Teacher ceiling on r2 | a teacher's score bounds what a student can learn | 10 min | ~$0.5 |
| 3. Relabel with Nova Pro | teacher + verifier = label pipeline; yield, cost | 30 min | ~$2-3 |
| 4. Managed fine-tune | Bedrock SFT job, hyperparameters, loss curves | 20 min + 1-2 h wait | ~$4 + storage |
| 5. Deploy + evaluate | on-demand custom model, the four metrics, reading failures | 30 min | <$0.5 |
| 6. Record results | the table and the answer to the question | 15 min | $0 |
| 7. Cleanup | leave nothing billing | 10 min | $0 |
| Optional: 4b, 8, 9 | self-labelled arm, batch inference, whole pipeline on Bedrock | | ~$4 / <$0.2 / pennies |

Quotas checked 2026-11-21: Nova Pro 25 requests/min cross-region (`us.` profile), 230M tokens/day;
Nova Micro custom deployments open. Budget cap stays $20; the main path is about $7.

---

## 1. Look at the data (no cost)

What was collected (automatic, done):

- `output/campaign/campaign-r1.jsonl`, `campaign-r2.jsonl`: one line per lab drill (fault injected,
  NetCortex's live RCA, and its score against the faults log). r1 = training round (20 drills),
  r2 = test round (21 drills, a different day), never used for training.
- `output/alarm_bridge/*.json`: the full RCA record behind each line.
- `output/finetune/r1-nova/`: ~1,040 verified **Nova Micro** labels (used for the self-labelled arm).
- `output/finetune/dataset/`: Nova Micro + Gemini labels split **by drill** (17 train / 3 val drills),
  `split.json` says which drill went where.
- `output/finetune/eval/prompts.jsonl`: the r2 test prompts; `pred-*.jsonl`: predictions, 3 per drill.

How a training example is made (`finetune/label_teacher.py`):

1. Rebuild the exact prompt NetCortex sent the LLM for a drill (`common.py` calls the synthesizer's
   own `_build_llm_prompt`).
2. Make variants: the same drill with the agents' findings in a different order (agents finish in
   any order in real life, and the evidence ids `E1, E2…` renumber with it).
3. Ask a teacher model for the RCA JSON, several times.
4. **Verify** every answer with NetCortex's own guards (citations must exist, the root cause must
   name the top-ranked cause) and the drill scorer (it must name the true culprit when the
   evidence holds it). Only answers that pass are kept.

So the teacher decides the *yield* (how many answers pass) and the *style* of the kept answers;
the verifier decides what counts as correct.

What the data is made of (r1, current dataset): every example is an incident, because NetCortex
only runs after an alarm. By verdict: cause 47%, symptom 37%, miss 8% (`nat-gateway-deleted`),
clean 8% (`mss-clamp-removed`, the negative control). The last two are one fault type each: the
known weak spot (see part 6).

```bash
cat output/finetune/dataset/split.json | jq '{train: (.train|keys), val: (.val|keys)}'
head -1 output/finetune/dataset/train.jsonl | jq -r '.messages[0].content' | head -60   # the prompt
head -1 output/finetune/dataset/train.jsonl | jq -r '.messages[1].content' | jq .      # the answer
```

Questions to answer for yourself:
- Why is the split by **drill**, not by sample? (60 variants of one incident are near-copies.)
- Why is r2 a separate **round**, not a random 20%? (A new day, new alarms, new noise.)
- What does the model see that a human on call would see, and what does it never see? (The faults
  log is the answer key: NetCortex and the training data never contain it.)

## 2. Teacher ceiling on r2 (~$0.5)

Before paying for labels, check the teacher is actually better than the student. `eval_round.sh`
is idempotent: arms already scored (Nova Micro, few-shot, Gemini) are skipped; only Nova Pro runs.

```bash
finetune/eval_round.sh output/campaign/campaign-r2.jsonl
jq -r '.summary[] | [.model, .strict_json, .accepted, .headline_raw, .headline_final] | @tsv' output/finetune/eval/results.json
```

Measured 2026-11-21 (r2, 63 samples = 21 drills x 3; API errors retried, not scored):

| model | strict_json | accepted | headline_raw |
|---|---|---|---|
| nova-micro | 46/63 | 52/63 | 43/51 |
| nova-micro-fewshot | 63/63 | 58/63 | 44/51 |
| nova-pro (teacher) | 0/63 | 59/63 | 50/51 |
| gemini-flash-lite | 63/63 | 32/63 | 48/51 |

Nova Pro's 0/63 strict JSON is a ```` ```json ```` fence around every answer, not bad JSON
(62/63 parse after stripping it). Training targets are re-serialized with `json.dumps`, so the
student learns plain JSON. The gate passed: the teacher names the culprit on 50/51 vs 44/51.

If Nova Pro isn't clearly above few-shot Micro on `accepted` and `headline_raw`, stop here: the
student can't learn what the teacher doesn't know, and that is a result worth writing up.

## 3. Relabel r1 with Nova Pro (~$2-3, ~25 min)

20 drills x 8 finding orders x 3 samples = 480 calls of ~4k input tokens (~2M tokens in, ~0.2M out).
Paced at 20 requests/min, under the 25/min quota.

```bash
.venv/bin/python finetune/label_teacher.py output/campaign/campaign-r1.jsonl --out output/finetune/r1-nova-pro \
  --provider bedrock --model us.amazon.nova-pro-v1:0 --rpm 20 --variants 8 --samples 3 --keep 20
```

It prints one line per drill: calls, accepted, good (accepted and names the culprit when it
should), kept. Compare the `teacher_accept_rate` with Nova Micro's 0.783
(`jq .teacher_accept_rate output/finetune/r1-nova/label_stats.json`). Check the `clean` and `miss`
drills got examples: those are the ones that teach "don't invent a culprit".

Split by drill (same seed, same drills, so the same 3 validation drills as before; the `diff`
proves it) and convert to Bedrock's format:

```bash
.venv/bin/python finetune/build_dataset.py output/finetune/r1-nova-pro/train.jsonl --out output/finetune/dataset-pro
diff <(jq -S '.val|keys' output/finetune/dataset/split.json) <(jq -S '.val|keys' output/finetune/dataset-pro/split.json) && echo same-val-drills
.venv/bin/python finetune/to_nova.py output/finetune/dataset-pro --out output/finetune/nova-pro --limit 300 --val-limit 40
```

`to_nova.py` takes an equal share per drill, writes `bedrock-conversation-2024` records with no
system turn (NetCortex sends the whole instruction in the user turn; training and inference
prompts must match), and checks Nova's rules: 8-20,000 records, 32k-token context, and the
reserved strings (`User:`, `Bot:`, `Assistant:`, `System:`, `<image>`, `<video>`, `[EOS]`) that
fail a job. It prints a cost estimate.

## 4. Managed fine-tune on Bedrock (~$4, 1-2 h wait)

Bucket (private), then a role Bedrock assumes to read the data and write results:

```bash
aws s3 mb s3://$BUCKET
aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3 cp output/finetune/nova-pro/train.jsonl s3://$BUCKET/nova/train.jsonl
aws s3 cp output/finetune/nova-pro/val.jsonl s3://$BUCKET/nova/val.jsonl

cat > /tmp/ctrust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock.amazonaws.com"},
 "Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCT"},
 "ArnLike":{"aws:SourceArn":"arn:aws:bedrock:us-east-1:$ACCT:model-customization-job/*"}}}]}
EOF
cat > /tmp/cs3.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/nova*"]},
 {"Effect":"Allow","Action":["s3:PutObject"],"Resource":["arn:aws:s3:::$BUCKET/nova*/output/*"]}]}
EOF
aws iam create-role --role-name netcortex-nova-ft --assume-role-policy-document file:///tmp/ctrust.json
aws iam put-role-policy --role-name netcortex-nova-ft --policy-name s3 --policy-document file:///tmp/cs3.json
CROLE=arn:aws:iam::$ACCT:role/netcortex-nova-ft
```

The job. Hyperparameters (API names): `epochCount` 1-5 (default 2), `learningRate` 1e-6..1e-4
(default 1e-5), `learningRateWarmupSteps` 0-100 (AWS suggests dataset size / 640 for Micro: ~1 here).
Keep the defaults: with one run you can't tune them, and defaults are what you'd defend in a review.

```bash
FTJOB=$(aws bedrock create-model-customization-job --job-name nova-micro-rca-pro --custom-model-name nova-micro-rca-pro \
  --role-arn $CROLE --base-model-identifier amazon.nova-micro-v1:0:128k --customization-type FINE_TUNING \
  --hyper-parameters epochCount=2,learningRate=0.00001,learningRateWarmupSteps=1 \
  --training-data-config s3Uri=s3://$BUCKET/nova/train.jsonl \
  --validation-data-config "validators=[{s3Uri=s3://$BUCKET/nova/val.jsonl}]" \
  --output-data-config s3Uri=s3://$BUCKET/nova/output/ --query jobArn --output text)
echo $FTJOB
aws bedrock get-model-customization-job --job-identifier $FTJOB --query '[status,failureMessage]'   # Validating -> InProgress -> Completed
```

While it trains, compare what you control here vs a self-run QLoRA (LAB-qwen.md): you pick
epochs, learning rate and warmup; AWS picks the method, rank, batch size and hardware. When it's
`Completed`, look at the loss curves (console: Custom models -> your model -> Training metrics, or
`s3://$BUCKET/nova/output/`). Validation loss going up while training loss goes down = overfitting.

### 4b. Optional: the self-labelled arm (~$4)

Same job on Nova Micro's **own** verified labels (`output/finetune/nova/`, built earlier from
`dataset/`). If it scores like the Pro-labelled model, the verifier did the work, not the teacher.

```bash
aws s3 cp output/finetune/nova/train.jsonl s3://$BUCKET/nova-self/train.jsonl
aws s3 cp output/finetune/nova/val.jsonl s3://$BUCKET/nova-self/val.jsonl
FTJOB_SELF=$(aws bedrock create-model-customization-job --job-name nova-micro-rca-self --custom-model-name nova-micro-rca-self \
  --role-arn $CROLE --base-model-identifier amazon.nova-micro-v1:0:128k --customization-type FINE_TUNING \
  --hyper-parameters epochCount=2,learningRate=0.00001,learningRateWarmupSteps=1 \
  --training-data-config s3Uri=s3://$BUCKET/nova-self/train.jsonl \
  --validation-data-config "validators=[{s3Uri=s3://$BUCKET/nova-self/val.jsonl}]" \
  --output-data-config s3Uri=s3://$BUCKET/nova-self/output/ --query jobArn --output text)
```

## 5. Deploy and evaluate (<$0.5)

On-demand deployment: no hourly charge, you pay per token like base Nova Micro. The custom model
itself has a monthly storage fee, so part 7 deletes it.

```bash
CUSTOM=$(aws bedrock get-model-customization-job --job-identifier $FTJOB --query outputModelArn --output text)
DEP=$(aws bedrock create-custom-model-deployment --model-deployment-name nova-micro-rca-pro \
  --model-arn $CUSTOM --query customModelDeploymentArn --output text)
aws bedrock get-custom-model-deployment --custom-model-deployment-identifier $DEP --query status   # Creating -> Active

TUNED_NOVA=$DEP finetune/eval_round.sh output/campaign/campaign-r2.jsonl
# with 4b: TUNED_NOVA=$DEP TUNED_NOVA_SELF=$DEP_SELF finetune/eval_round.sh output/campaign/campaign-r2.jsonl
.venv/bin/python finetune/eval_models.py score output/campaign/campaign-r2.jsonl output/finetune/eval/pred-*.jsonl --failures
```

| column | meaning |
|---|---|
| `strict_json` | reply parses with plain `json.loads`, which is what NetCortex does |
| `accepted` | NetCortex would keep the model's root cause: its citations exist and it names the top-ranked cause |
| `headline_raw` | on drills whose evidence holds the culprit: the model's own sentence names it |
| `headline_final` | what NetCortex finally reports (it falls back to the rules sentence when it rejects the model's) |

Counts are `passed/total` (21 test drills x 3 samples: one drill is ~5%, so read failures, not
percentages). Then read 3-4 answers by hand, including the no-cause faults (`mtu-blackhole`,
`mss-clamp-removed`): does the model invent a culprit?

Known bias to report honestly: NetCortex's citation check only accepts `[E4]`, not `[E4, E17]`.
Gemini often writes the second form and gets rejected for format, not reasoning.

## 6. Record the results (for the write-up)

Fill this in from `results.json`:

| arm | strict_json | accepted | headline_raw | headline_final | cost per 1k RCAs |
|---|---|---|---|---|---|
| nova-micro | 46/63 | 52/63 | 43/51 | 46/51 | |
| nova-micro-fewshot | 63/63 | 58/63 | 44/51 | 48/51 | |
| nova-pro | | | | | |
| nova-micro-tuned | | | | | |
| nova-micro-tuned-self (opt) | | | | | |

Answer in one sentence each:
1. Does tuning beat few-shot on `accepted` and `headline_raw`? By how many drills, not percent?
2. How close does the student get to the teacher?
3. What does tuning buy even at equal quality? Prompt size (few-shot adds 3 full examples, ~12k
   tokens) and therefore cost and latency per RCA.
4. What did no model fix? (Usually the uncovered faults: evidence that isn't there can't be cited.)

Limits to state: 20 training drills from one lab, one test round, 3 samples each; negative
controls are one fault type each (`clean`, `miss`), so "doesn't invent a culprit" is weakly tested.

### Results, 2026-11-21 (run 1)

Tune: Nova Micro, 300 train / 40 val Nova Pro labels, 2 epochs, lr 1e-5. Job 2h50m wall
(~2h queued for capacity), ~$4. Train loss 0.33 -> 0.17; val loss 0.186 (epoch 1), 0.184
(epoch 2): flat, no overfit. Total spend for the experiment under $10.

**r2 test round** (21 drills x 3, never trained on):

| arm | strict_json | accepted | headline_raw | headline_final | input size |
|---|---|---|---|---|---|
| gemini-flash-lite (ref) | 63/63 | 32/63 | 48/51 | 51/51 | 1x |
| nova-micro | 46/63 | 52/63 | 43/51 | 46/51 | 1x |
| nova-micro-fewshot | 63/63 | 58/63 | 44/51 | 48/51 | ~2.9x |
| nova-micro-fewshot + abstain line | 63/63 | 56/63 | 45/51 | 51/51 | ~2.9x |
| nova-pro (teacher) | 0/63 (\`\`\`json fences) | 59/63 | 50/51 | 51/51 | 1x, ~20x Micro price |
| **nova-micro-tuned** | **63/63** | **62/63** | **48/51** | 49/51 | 1x |

Tuned misses: `phz-record-deleted` x2, `frr-max-prefix` x1 (few-shot missed those plus 5 other
drills). On the negative control `mss-clamp-removed` it says "no clear root cause" 1/3 and blames
the VPN 2/3.

**False-alarm probe** (`false_alarms.py`: 5 r2 drills stripped to alarm-entity symptoms; 15 probes
x 3 = 45 answers per arm; right answer is always "no specific root cause"):

| arm | prompt | abstains | notes recovery | blames planted DNS change |
|---|---|---|---|---|
| nova-micro | original | 0/45 | 0/45 | 14/15 |
| nova-micro-fewshot | original | 0/45 | 0/45 | 14/15 |
| nova-pro | original | 0/45 | 0/45 | 15/15 |
| nova-micro-tuned | original | 0/45 | 0/45 | 15/15 |
| nova-micro | + abstain line | 0/45 | 11/45 | 14/15 |
| nova-micro-fewshot | + abstain line | 15/45 | 24/45 | 3/15 |
| nova-pro | + abstain line | 30/45 | 16/45 | 0/15 |
| **nova-micro-tuned** | + abstain line | **0/45** | 1/45 | **14/15** |

Answers:
1. Tuning beats few-shot: accepted 62 vs 58 (4 answers), headline_raw 48 vs 44 (4 answers,
   5 drills fewer with any miss), at a third of the input tokens.
2. The student nearly matches the teacher on the task (48/51 vs 50/51 headline, better accepted
   and strict JSON) at Micro price.
3. Even at equal quality: ~2.9x shorter input than few-shot, no prompt examples to maintain.
4. No model fixes false alarms under the original prompt: the instruction says "name the specific
   root cause", the guard wants the top cause named, and every label was made under that prompt.
   Even Nova Pro blames a harmless DNS TXT upsert 15/15.
5. **The cost of tuning: it froze the behaviour.** One added sentence fixes Pro (0/15 herring) and
   mostly fixes few-shot Micro (3/15), but the tuned model ignores it (14/15, 0 abstains). It
   learned "always blame something" from labels built under the old prompt. Every prompt fix now
   needs a relabel + retrain, so the label set and the prompt must be versioned together.

Verdict: fine-tuning pays off for the narrow, stable task (format, citations, naming the culprit),
and the verifier is what made the labels trustworthy. It is the wrong tool for a behaviour you're
still changing. Product fix, in order: add the abstain sentence to the synthesizer's root_cause
instruction, add negative-control and false-alarm labels that abstain, then retrain if tuning is
kept. Future work: a live false-alarm campaign (transient blips, harmless changes) on the lab.

## 7. Cleanup

```bash
aws bedrock delete-custom-model-deployment --custom-model-deployment-identifier $DEP
aws bedrock delete-custom-model --model-identifier nova-micro-rca-pro
aws bedrock delete-custom-model --model-identifier nova-micro-rca-self 2>/dev/null   # 4b
aws s3 rm s3://$BUCKET --recursive && aws s3 rb s3://$BUCKET
aws iam delete-role-policy --role-name netcortex-nova-ft --policy-name s3 && aws iam delete-role --role-name netcortex-nova-ft
aws iam delete-role-policy --role-name netcortex-batch --policy-name batch 2>/dev/null && aws iam delete-role --role-name netcortex-batch 2>/dev/null
aws bedrock list-custom-models --query 'length(modelSummaries)'                     # 0
aws bedrock list-custom-model-deployments --query 'length(modelDeploymentSummaries)'   # 0
```

---

## 8. Optional: batch inference (label in bulk at about half price)

On-demand labeling paces around rate limits. In production you'd send it as one batch job:
cheaper, no rate-limit juggling, results in minutes to hours. Input (already built by
`finetune/batch.py make`): 120 Converse-format records (Bedrock needs at least 100 per job).
Do this before part 7 (it uses the bucket).

```bash
aws s3 cp output/finetune/batch/input.jsonl s3://$BUCKET/batch/input/input.jsonl
cat > /tmp/btrust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock.amazonaws.com"},
 "Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCT"},
 "ArnLike":{"aws:SourceArn":"arn:aws:bedrock:us-east-1:$ACCT:model-invocation-job/*"}}}]}
EOF
cat > /tmp/bpol.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/batch/*"]},
 {"Effect":"Allow","Action":["s3:PutObject"],"Resource":["arn:aws:s3:::$BUCKET/batch/output/*"]},
 {"Effect":"Allow","Action":["bedrock:InvokeModel"],"Resource":["arn:aws:bedrock:*::foundation-model/amazon.nova-micro-v1:0",
   "arn:aws:bedrock:us-east-1:$ACCT:inference-profile/us.amazon.nova-micro-v1:0"]}]}
EOF
aws iam create-role --role-name netcortex-batch --assume-role-policy-document file:///tmp/btrust.json
aws iam put-role-policy --role-name netcortex-batch --policy-name batch --policy-document file:///tmp/bpol.json

BJOB=$(aws bedrock create-model-invocation-job --job-name rca-label-r1 --model-id us.amazon.nova-micro-v1:0 \
  --model-invocation-type Converse --role-arn arn:aws:iam::$ACCT:role/netcortex-batch \
  --input-data-config "s3InputDataConfig={s3Uri=s3://$BUCKET/batch/input/}" \
  --output-data-config "s3OutputDataConfig={s3Uri=s3://$BUCKET/batch/output/}" --query jobArn --output text)
aws bedrock get-model-invocation-job --job-identifier $BJOB --query '[status,message]'   # Submitted -> InProgress -> Completed
mkdir -p output/finetune/batch/out && aws s3 cp s3://$BUCKET/batch/output/ output/finetune/batch/out/ --recursive
.venv/bin/python finetune/batch.py collect output/campaign/campaign-r1.jsonl output/finetune/batch \
  --outputs $(find output/finetune/batch/out -name '*.jsonl.out')
```

Compare the `accepted` rate with the on-demand labeling (78% for Nova Micro). If the
inference-profile id isn't accepted for batch, use the foundation model id `amazon.nova-micro-v1:0`.

## 9. Optional: NetCortex itself on Bedrock (the real behaviour check)

Everything above scores the synthesis step in isolation. The final check is the whole pipeline
(classification, agents, peer questions, hypothesis triage, synthesis) with your model as the LLM,
replaying r2's recorded telemetry, so the lab doesn't have to be up. Do this before part 7.

```bash
python - <<'EOF'
import yaml
c = yaml.safe_load(open("config/config.aws-lab.yaml"))
c["llm"] = {"provider": "bedrock", "model": "PUT_MODEL_ID_OR_ARN_HERE", "region": "us-east-1"}
yaml.safe_dump(c, open("/tmp/config.bedrock.yaml", "w"), sort_keys=False)
EOF
# model: us.amazon.nova-micro-v1:0, or $DEP (tuned Nova)
GEMINI_API_KEY= GOOGLE_CLOUD_PROJECT= NETCORTEX_CATALOG_DIR=config/catalog.aws-lab \
  .venv/bin/python -m evals.replay_recordings $(jq -r 'select(.record != null and .record != "") | .record' output/campaign/campaign-r2.jsonl) \
  --config /tmp/config.bedrock.yaml --truth output/campaign/campaign-r2.jsonl > output/finetune/eval/replay-<name>.jsonl
jq -s 'map({fault, verdict, headline_cause, llm_used}) ' output/finetune/eval/replay-<name>.jsonl
```

Tested on 2026-11-20 with Nova Micro on one r2 drill (`nat-route-removed`): verdict `cause`, the
headline named the culprit, the LLM's own sentence was kept, and a Nova-proposed hypothesis was
refuted by the rules.

## Pitfalls already hit

| Symptom | Cause | Fix |
|---|---|---|
| Gemini `429 RESOURCE_EXHAUSTED` | free tier: 15 requests/min per model | pace calls (`--rpm`); Bedrock does bulk labeling |
| Bedrock `Too many tokens per day` | new-account quotas at 0 | raise quotas / support case (done) |
| Bedrock `ThrottlingException` on Nova Pro | 25 requests/min cross-region | `--rpm 20` for labeling, `--rpm 12` for eval |
| Fine-tune job fails validation | reserved strings, >32k tokens, or a system turn mismatch | `to_nova.py` checks all three before upload |
