# Hands-on lab: fine-tune, evaluate and host a small RCA model

You will train **Qwen2.5-7B-Instruct** (LoRA) to write NetCortex's RCA synthesis,
test it on drills it has never seen, compare it with Gemini Flash Lite and Nova
Micro, then host it on **Amazon Bedrock** (Custom Model Import) and check that it
behaves the same there.

Run everything from the repo root in WSL unless a step says "on the box".
Region us-east-1, profile `aws-veldanda-admin` (`export AWS_PROFILE=aws-veldanda-admin AWS_REGION=us-east-1 AWS_PAGER=`).

| Part | What you learn | Time | Cost |
|---|---|---|---|
| 1. Look at the data | what one training example is; why the split is by drill | 10 min | $0 |
| 2. Launch a GPU box | DLAMI, instance type vs quota, capacity errors | 15 min | ~$1.9/h from here |
| 3. Set up + smoke test | catching problems on 3 examples first | 15 min | |
| 4. Train | LoRA on a 4-bit model, train vs validation loss | ~2 h (g6e.xlarge) | ~$4 |
| 5. Evaluate | held-out test, the metrics, comparing 4 models | 30 min | ~$1 |
| 6. Merge + upload | adapter vs merged weights; S3 | 15 min | <$0.5 |
| 7. Host on Bedrock | import role, import job, Converse, cold start | 30 min | pay per active minute |
| 8. Clean up | leave nothing billing | 10 min | |

**Stop rule:** if anything costs more time than planned, terminate the box (part 8) and
come back; the data and scripts are all local.

---

## 1. Look at the data (no cost)

What was collected (automatic, done):

- `output/campaign/campaign-r1.jsonl`, `campaign-r2.jsonl`: one line per lab drill (fault injected,
  NetCortex's live RCA, and its score against the faults log).
- `output/alarm_bridge/*.json`: the full RCA record behind each line.
- `output/finetune/dataset/{train,val}.jsonl`: the training data, `split.json`: which drill went where.
- `output/finetune/eval/prompts.jsonl`: the **test set**, round r2, never used for training.
- `output/finetune/eval/pred-gemini-flash-lite.jsonl`, `pred-nova-micro.jsonl`: baselines, already run.

How a training example was made (`finetune/label_teacher.py`):

1. Rebuild the exact prompt NetCortex sent the LLM for a drill (`finetune/common.py` calls the
   synthesizer's own `_build_llm_prompt`).
2. Make variants: the same drill with the agents' findings in a different order (agents finish in
   any order in real life, and the evidence ids `E1, E2…` renumber with it).
3. Ask a teacher model (Nova Micro, Gemini Flash Lite) for the RCA JSON, several times.
4. **Verify** every answer with NetCortex's own guards (citations must exist, the root cause must
   name the top-ranked cause) and the drill scorer (it must name the true culprit when the
   evidence holds it). Only answers that pass are kept.

Try it:

```bash
cat output/finetune/dataset/split.json | jq '{train: (.train|keys), val: (.val|keys)}'
head -1 output/finetune/dataset/train.jsonl | jq -r '.messages[0].content' | head -60   # the prompt
head -1 output/finetune/dataset/train.jsonl | jq -r '.messages[1].content' | jq .      # the answer
head -1 output/finetune/dataset/train.jsonl | jq .meta
```

Questions to answer for yourself:
- Why is the split by **drill**, not by sample? (Hint: 60 variants of one incident are near-copies.)
- Why is r2 a separate **round**, not a random 20%? (Hint: a new day, new alarms, new noise.)
- What does the model see that a human on call would see, and what does it never see? (The faults
  log is the answer key: NetCortex and the training data never contain it.)

## 2. Launch a GPU box

Quota: "Running On-Demand G and VT instances" = 8 vCPUs, so either **g6e.xlarge** (L40S 48 GB,
4 vCPU, faster) or **g6.2xlarge** (L4 24 GB, 8 vCPU, ~2x slower, cheaper per hour).

```bash
export AWS_PROFILE=aws-veldanda-admin AWS_REGION=us-east-1 AWS_PAGER=
MYIP=$(curl -s https://checkip.amazonaws.com)
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)

# SSH key and a security group that only lets your IP in
aws ec2 create-key-pair --key-name netcortex-ft --key-type ed25519 --query KeyMaterial --output text > ~/.ssh/netcortex-ft.pem
chmod 600 ~/.ssh/netcortex-ft.pem
SG=$(aws ec2 create-security-group --group-name netcortex-ft-ssh --description "SSH for fine-tuning box" \
     --vpc-id $VPC --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 22 --cidr $MYIP/32

# Deep Learning AMI (PyTorch, Ubuntu 24.04): look it up, never hard-code AMI ids
AMI=$(aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-pytorch-2.12-ubuntu-24.04/latest/ami-id \
      --query Parameter.Value --output text)

# Cost cap: the box powers itself off after 8 hours whatever happens
UD=$(printf '#!/bin/bash\nshutdown -h +480\n' | base64 -w0)

for AZ in us-east-1a us-east-1b us-east-1c us-east-1d; do
  SUBNET=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC Name=availability-zone,Values=$AZ --query 'Subnets[0].SubnetId' --output text)
  ID=$(aws ec2 run-instances --image-id $AMI --instance-type g6e.xlarge --key-name netcortex-ft \
    --security-group-ids $SG --subnet-id $SUBNET --associate-public-ip-address \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=200,VolumeType=gp3,DeleteOnTermination=true}' \
    --instance-initiated-shutdown-behavior stop --metadata-options HttpTokens=required --user-data $UD \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=netcortex-ft},{Key=Project,Value=netcortex-ft}]' \
    --query 'Instances[0].InstanceId' --output text) && break
done
echo $ID
aws ec2 wait instance-running --instance-ids $ID
IP=$(aws ec2 describe-instances --instance-ids $ID --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
ssh -i ~/.ssh/netcortex-ft.pem ubuntu@$IP nvidia-smi
```

Expect: `InsufficientInstanceCapacity` in some AZs. That is AWS being out of that GPU there,
not your quota; the loop tries the next AZ. (Yesterday 1a/1b/1c were out of g6, 1d worked.)

## 3. Set up the box and smoke-test

```bash
K="-i $HOME/.ssh/netcortex-ft.pem"
scp $K finetune/train_lora.py finetune/generate.py finetune/requirements.txt ubuntu@$IP:~/
ssh $K ubuntu@$IP mkdir -p ft
scp $K output/finetune/dataset/train.jsonl output/finetune/dataset/val.jsonl ubuntu@$IP:~/ft/
ssh $K ubuntu@$IP
```

On the box:

```bash
source /opt/pytorch/bin/activate
pip install -q -r requirements.txt "huggingface_hub[cli]"
python -c "from huggingface_hub import snapshot_download as s; print(s('Qwen/Qwen2.5-7B-Instruct'))"   # ~15 GB, ~5 min

# smoke test: 3 examples, 1 epoch, ~2 min. Fix problems here, not 2 hours into a run.
head -3 ft/train.jsonl > ft/smoke.jsonl
python train_lora.py --data ft/smoke.jsonl --out ft/smoke --epochs 1 --max-len 12288
```

Expect `3 examples, max tokens ~11000`, one `{'loss': ...}` line around 1.0, then `adapter:` and
`merged:` paths. Then `rm -rf ft/smoke`.

## 4. Train (run it in tmux so a dropped SSH doesn't kill it)

```bash
tmux new -s train
source /opt/pytorch/bin/activate
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python train_lora.py \
  --data ft/train.jsonl --val ft/val.jsonl --out ft/qwen7b --epochs 1 --max-len 12288 --eval-steps 20 2>&1 | tee ft/train.log
# detach: Ctrl-b d      re-attach: tmux attach -t train
```

What the knobs mean (`train_lora.py`):
- **4-bit base (QLoRA):** the 7B model's weights are frozen and stored in 4 bits; only small LoRA
  matrices (rank 16, ~40M parameters, 0.5%) are trained. That is why it fits one GPU.
- **Answer-only loss:** the prompt (up to ~12k tokens) is masked; the model is graded only on the
  RCA JSON it writes. Logits are computed only for the answer span (`AnswerOnlyTrainer`): full
  logits over a 152k vocabulary would need ~7 GB more and ran out of memory on the L4.
- **Effective batch 8** (batch 1 x gradient accumulation 8), cosine learning rate from 2e-4, 1 epoch.

Read the log while it runs (`grep -E "'loss'|eval_loss" ft/train.log`):
- `loss` (training) should fall from ~1.0 towards ~0.3-0.5.
- `eval_loss` (validation drills the model never trains on) should fall too. If training loss keeps
  falling but `eval_loss` turns upward, the model is memorising the training drills: stop and use
  fewer steps or a smaller learning rate.

Output: `ft/qwen7b/adapter` (LoRA weights, ~165 MB) and `ft/qwen7b/merged` (full model, ~15 GB).

### 4b. Experiments: change one knob at a time (read [CONCEPTS.md](CONCEPTS.md) first)

Short runs on a subset, adapter only, each into its own folder:

```bash
for run in "A --method qlora --rank 16" "B --rank 4" "C --rank 64" "D --method qdora --rank 16"; do
  set -- $run; name=$1; shift
  python train_lora.py --data ft/train.jsonl --val ft/val.jsonl --out ft/exp-$name --epochs 1 \
    --limit 300 --no-merge --max-len 12288 --eval-steps 10 "$@" 2>&1 | tee ft/exp-$name.log
done
grep -H "eval_loss" ft/exp-*.log | tail -n 8          # last validation loss per run
grep -H "train_samples_per_second" ft/exp-*.log      # speed per run
```

Score each on the test round (from WSL), e.g. for run D:

```bash
scp $K output/finetune/eval/prompts.jsonl ubuntu@$IP:ft/
ssh $K ubuntu@$IP "source /opt/pytorch/bin/activate && python generate.py --prompts ft/prompts.jsonl --adapter ft/exp-D/adapter --out ft/pred-exp-D.jsonl"
scp $K ubuntu@$IP:ft/pred-exp-D.jsonl output/finetune/eval/
.venv/bin/python finetune/eval_models.py score output/campaign/campaign-r2.jsonl output/finetune/eval/pred-*.jsonl
```

## 5. Evaluate on the test round (r2)

From WSL (the script copies prompts to the box, runs base and tuned Qwen, copies results back;
Gemini and Nova predictions already exist so it skips them):

```bash
ADAPTER='~/ft/qwen7b/adapter' finetune/eval_round.sh output/campaign/campaign-r2.jsonl ubuntu@$IP
```

You get one row per model:

| column | meaning |
|---|---|
| `strict_json%` | reply parses with plain `json.loads`, which is what NetCortex does |
| `accepted%` | NetCortex would keep the model's root cause: its citations exist and it names the top-ranked cause |
| `headline_raw%` | on drills whose evidence holds the culprit: the model's own sentence names it |
| `headline_final%` | what NetCortex finally reports (it falls back to the rules sentence when it rejects the model's) |

What to look for: does tuned Qwen beat base Qwen on `strict_json%` and `accepted%` (format and
discipline), and does it match Gemini/Nova on `headline_raw%` (reasoning)? Then look at failures:

```bash
jq -r '.detail["qwen-tuned"][] | select(.accepted|not) | .fault' output/finetune/eval/results.json
```

Known bias to report honestly: NetCortex's citation check only accepts `[E4]`, not `[E4, E17]`.
Gemini often writes the second form and gets rejected for format, not reasoning; the tuned model
learned the accepted form from verified examples.

## 6. Merge and upload to S3

`train_lora.py` already wrote the merged model (`ft/qwen7b/merged`: safetensors, `config.json`,
tokenizer). That is the Hugging Face format Bedrock imports. On the box:

```bash
ls -lh ft/qwen7b/merged
```

From WSL, make a bucket and give the box a way to upload (simplest: upload from the box with
temporary credentials you paste, or copy to WSL first; the merged model is ~15 GB, so upload from
the box):

```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=netcortex-ft-$ACCT
aws s3 mb s3://$BUCKET
aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
eval "$(aws configure export-credentials --format env)"   # short-lived SSO credentials for this shell
ssh $K ubuntu@$IP "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN aws s3 sync ft/qwen7b/merged s3://$BUCKET/qwen7b-r1/ --region us-east-1"
```

Once it's in S3 you can terminate the box (part 8) before the Bedrock part: hosting doesn't need it.

## 7. Host on Bedrock and verify behavior

**7a. A role Bedrock can use to read the bucket** (trust `bedrock.amazonaws.com`, limited to your
account's import jobs; read-only on the bucket):

```bash
cat > /tmp/trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock.amazonaws.com"},
 "Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCT"},
 "ArnLike":{"aws:SourceArn":"arn:aws:bedrock:us-east-1:$ACCT:model-import-job/*"}}}]}
EOF
cat > /tmp/s3read.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],
 "Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/*"],
 "Condition":{"StringEquals":{"aws:ResourceAccount":"$ACCT"}}}]}
EOF
aws iam create-role --role-name netcortex-model-import --assume-role-policy-document file:///tmp/trust.json
aws iam put-role-policy --role-name netcortex-model-import --policy-name s3read --policy-document file:///tmp/s3read.json
ROLE=arn:aws:iam::$ACCT:role/netcortex-model-import
```

**7b. Import** (no charge for the import itself; takes ~10-20 min):

```bash
JOB=$(aws bedrock create-model-import-job --job-name qwen7b-rca-r1 --imported-model-name qwen7b-rca-r1 \
  --role-arn $ROLE --model-data-source "{\"s3DataSource\":{\"s3Uri\":\"s3://$BUCKET/qwen7b-r1/\"}}" \
  --query jobArn --output text)
aws bedrock get-model-import-job --job-identifier $JOB --query '[status,failureMessage]'   # repeat until Completed
MODEL=$(aws bedrock list-imported-models --query "modelSummaries[?modelName=='qwen7b-rca-r1'].modelArn" --output text)
```

**7c. First call** (expect `ModelNotReadyException` on the very first one: a cold start of up to a
minute; just retry):

```bash
aws bedrock-runtime converse --model-id $MODEL \
  --messages '[{"role":"user","content":[{"text":"reply with OK"}]}]' --inference-config maxTokens=20
```

**7d. Verify behavior = same test, now through Bedrock.** The hosted model should score like it did
on the GPU box (small differences come from sampling settings):

```bash
.venv/bin/python finetune/eval_models.py bedrock output/finetune/eval/prompts.jsonl \
  --model $MODEL --out output/finetune/eval/pred-qwen-tuned-bedrock.jsonl --rpm 20
.venv/bin/python finetune/eval_models.py score output/campaign/campaign-r2.jsonl output/finetune/eval/pred-*.jsonl \
  --out output/finetune/eval/results.json
```

Billing: per active model copy per minute, in 5-minute increments from the first successful call;
it scales to zero after 5 idle minutes. Check the Bedrock pricing page for the per-minute rate
before you start, and keep this part to one sitting.

## 8. Clean up (check every line)

```bash
aws ec2 terminate-instances --instance-ids $ID && aws ec2 wait instance-terminated --instance-ids $ID
aws ec2 delete-security-group --group-id $SG
aws ec2 delete-key-pair --key-name netcortex-ft && rm -f ~/.ssh/netcortex-ft.pem
aws bedrock delete-imported-model --model-identifier qwen7b-rca-r1
aws s3 rm s3://$BUCKET --recursive && aws s3 rb s3://$BUCKET
aws iam delete-role-policy --role-name netcortex-model-import --policy-name s3read
aws iam delete-role --role-name netcortex-model-import
# nothing left:
aws ec2 describe-instances --filters Name=tag:Project,Values=netcortex-ft Name=instance-state-name,Values=pending,running,stopped --query 'length(Reservations)'
aws bedrock list-imported-models --query 'length(modelSummaries)'
```

---

## Pitfalls already hit (so you recognise them)

| Symptom | Cause | Fix (already in the scripts) |
|---|---|---|
| `max tokens 2` at the start of training | transformers 5 `apply_chat_template(tokenize=True)` returns a dict | render the template to text, then tokenize |
| `unexpected keyword argument 'warmup_ratio'` | removed in transformers 5 | `warmup_steps=2` |
| `CUDA out of memory ... Tried to allocate 6.62 GiB` in the loss | full-sequence logits over 152k vocab | `AnswerOnlyTrainer` computes logits for the answer only |
| training loss ~8 instead of ~1 | Trainer summed loss over gradient accumulation | `model_accepts_loss_kwargs = False` |
| Gemini `429 RESOURCE_EXHAUSTED` | free tier: 15 requests/min per model | pace calls (`--rpm`), Nova does bulk labeling |
| `InsufficientInstanceCapacity` | no GPUs of that type in that AZ right now | try other AZs |
| Bedrock `Too many tokens per day` | new-account quotas at 0 | raise quotas / support case (done) |

## Pilot run (for reference)

A pilot on 96 Gemini-labelled samples (2 epochs, g6.2xlarge, 43 min) is in `output/finetune/pilot/`
(`qwen7b-r1/train.log`, partial r2 eval). Its loss numbers are on the old summed scale (divide by 8).
