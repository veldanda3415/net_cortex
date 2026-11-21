#!/bin/bash
# eval_round.sh <campaign.jsonl> [ssh-host]: idempotent eval of every arm on every scored
# drill in the round. Each run only fills what's missing: new drills, and samples that
# failed with an API error (429/503 etc. are dropped and retried, never scored as bad JSON).
#   SAMPLES     samples per drill for the API arms (default 3; Qwen is greedy, 1)
#   TUNED_NOVA  deployment ARN of the Nova Micro tuned on Nova Pro labels (arm nova-micro-tuned)
#   TUNED_NOVA_SELF  deployment ARN of the Nova Micro tuned on its own labels (arm nova-micro-tuned-self)
#   ssh-host    only for the optional Qwen arms on a GPU box (LAB-qwen.md); omit to skip them
#   ADAPTER     adapter dir on the box (default ~/ft/qwen7b-r1/adapter)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LINES=${1:?campaign jsonl}; HOST=${2:-}
OUT=$ROOT/output/finetune/eval; mkdir -p "$OUT"
PY=$ROOT/.venv/bin/python
SSH=(ssh -i "$HOME/.ssh/netcortex-ft.pem" -o ServerAliveInterval=30 "$HOST")
ADAPTER=${ADAPTER:-'~/ft/qwen7b-r1/adapter'}
SAMPLES=${SAMPLES:-${GEMINI_SAMPLES:-3}}

"$PY" "$ROOT/finetune/eval_models.py" prompts "$LINES" --out "$OUT/prompts.jsonl" 2>/dev/null

todo() {  # todo <pred file> <samples>: drop error lines, emit each prompt (samples - have) times
  "$PY" - "$OUT/prompts.jsonl" "$1" "$2" <<'EOF'
import json, sys
from collections import Counter
prompts, pred, want = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    rows = [json.loads(x) for x in open(pred) if x.strip()]
except FileNotFoundError:
    rows = []
good = [r for r in rows if not str(r.get("output", "")).startswith("__error__")]
with open(pred, "w") as fh:
    for r in good:
        fh.write(json.dumps(r) + "\n")
have = Counter(r["case"] for r in good)
for line in open(prompts):
    p = json.loads(line)
    for _ in range(max(0, want - have[p["case"]])):
        print(line.rstrip())
EOF
}

api_arm() {  # api_arm <name> <eval_models.py args...>
  local name=$1; shift
  todo "$OUT/pred-$name.jsonl" "$SAMPLES" > "$OUT/todo-$name.jsonl"
  [ -s "$OUT/todo-$name.jsonl" ] || return 0
  echo "$name: $(wc -l < "$OUT/todo-$name.jsonl") calls"
  "$PY" "$ROOT/finetune/eval_models.py" "$@" "$OUT/todo-$name.jsonl" --out "$OUT/new-$name.jsonl" 2>/dev/null
  cat "$OUT/new-$name.jsonl" >> "$OUT/pred-$name.jsonl"
}

api_arm gemini-flash-lite gemini                       # paced: free tier is 15 requests/min
api_arm nova-micro bedrock
api_arm nova-micro-fewshot bedrock --fewshot 3 --fewshot-from "$ROOT/output/finetune/dataset/train.jsonl"
api_arm nova-pro bedrock --model us.amazon.nova-pro-v1:0 --rpm 12   # the teacher: the ceiling for the student
[ -z "${TUNED_NOVA:-}" ] || api_arm nova-micro-tuned bedrock --model "$TUNED_NOVA"
[ -z "${TUNED_NOVA_SELF:-}" ] || api_arm nova-micro-tuned-self bedrock --model "$TUNED_NOVA_SELF"

# optional: Qwen base and tuned on the GPU box, greedy (1 sample)
for arm in qwen-base qwen-tuned; do
  [ -n "$HOST" ] || continue
  todo "$OUT/pred-$arm.jsonl" 1 > "$OUT/todo-$arm.jsonl"
  [ -s "$OUT/todo-$arm.jsonl" ] || continue
  scp -q -i "$HOME/.ssh/netcortex-ft.pem" "$OUT/todo-$arm.jsonl" "$HOST:ft/todo-$arm.jsonl"
  extra=""; [ "$arm" = qwen-tuned ] && extra="--adapter $ADAPTER"
  "${SSH[@]}" "source /opt/pytorch/bin/activate && python generate.py --prompts ~/ft/todo-$arm.jsonl --out ~/ft/new-$arm.jsonl $extra 2>&1 | grep -E '^\[' | tail -n 3"
  "${SSH[@]}" "cat ~/ft/new-$arm.jsonl" >> "$OUT/pred-$arm.jsonl"
done

"$PY" "$ROOT/finetune/eval_models.py" score "$LINES" "$OUT"/pred-*.jsonl --out "$OUT/results.json" 2>/dev/null
