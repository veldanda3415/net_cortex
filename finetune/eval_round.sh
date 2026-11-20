#!/bin/bash
# eval_round.sh <campaign.jsonl> <ssh-host>: idempotent eval of base Qwen, tuned Qwen
# and Gemini Flash Lite on every scored drill in the round; only new drills are
# predicted on each run, so it can run repeatedly while the campaign is still going.
#   ADAPTER  adapter dir on the box (default ~/ft/qwen7b-r1/adapter)
#   GEMINI_SAMPLES  Gemini samples per drill (default 3; Qwen is greedy)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LINES=${1:?campaign jsonl}; HOST=${2:?ubuntu@ip}
OUT=$ROOT/output/finetune/eval; mkdir -p "$OUT"
PY=$ROOT/.venv/bin/python
SSH=(ssh -i "$HOME/.ssh/netcortex-ft.pem" -o ServerAliveInterval=30 "$HOST")
ADAPTER=${ADAPTER:-'~/ft/qwen7b-r1/adapter'}

"$PY" "$ROOT/finetune/eval_models.py" prompts "$LINES" --out "$OUT/prompts.jsonl" 2>/dev/null
todo() {  # prompts whose case is not yet in predictions file $1
  [ -f "$1" ] || touch "$1"
  jq -c --slurpfile done <(jq -c .case "$1") 'select(.case as $c | ($done | index($c)) | not)' "$OUT/prompts.jsonl"
}

# Gemini (here, paced to share the free-tier quota with live RCAs)
todo "$OUT/pred-gemini-flash-lite.jsonl" > "$OUT/todo-gemini.jsonl"
if [ -s "$OUT/todo-gemini.jsonl" ]; then
  for _ in $(seq 1 "${GEMINI_SAMPLES:-3}"); do cat "$OUT/todo-gemini.jsonl"; done > "$OUT/todo-gemini-x.jsonl"
  "$PY" "$ROOT/finetune/eval_models.py" gemini "$OUT/todo-gemini-x.jsonl" --out "$OUT/new-gemini.jsonl" 2>/dev/null
  cat "$OUT/new-gemini.jsonl" >> "$OUT/pred-gemini-flash-lite.jsonl"
fi

# Nova Micro on Bedrock (3 samples, like Gemini)
todo "$OUT/pred-nova-micro.jsonl" > "$OUT/todo-nova.jsonl"
if [ -s "$OUT/todo-nova.jsonl" ]; then
  for _ in $(seq 1 "${GEMINI_SAMPLES:-3}"); do cat "$OUT/todo-nova.jsonl"; done > "$OUT/todo-nova-x.jsonl"
  "$PY" "$ROOT/finetune/eval_models.py" bedrock "$OUT/todo-nova-x.jsonl" --out "$OUT/new-nova.jsonl" 2>/dev/null
  cat "$OUT/new-nova.jsonl" >> "$OUT/pred-nova-micro.jsonl"
fi

# Nova Micro with 3 verified training examples first: does prompting alone close the gap?
todo "$OUT/pred-nova-micro-fewshot.jsonl" > "$OUT/todo-nova-fs.jsonl"
if [ -s "$OUT/todo-nova-fs.jsonl" ]; then
  for _ in $(seq 1 "${GEMINI_SAMPLES:-3}"); do cat "$OUT/todo-nova-fs.jsonl"; done > "$OUT/todo-nova-fs-x.jsonl"
  "$PY" "$ROOT/finetune/eval_models.py" bedrock "$OUT/todo-nova-fs-x.jsonl" --fewshot 3 \
    --fewshot-from "$ROOT/output/finetune/dataset/train.jsonl" --out "$OUT/new-nova-fs.jsonl" 2>/dev/null
  cat "$OUT/new-nova-fs.jsonl" >> "$OUT/pred-nova-micro-fewshot.jsonl"
fi

# Qwen base and tuned (GPU box, sequential); SKIP_QWEN=1 while the box is stopped
for arm in qwen-base qwen-tuned; do
  [ -z "${SKIP_QWEN:-}" ] || continue
  todo "$OUT/pred-$arm.jsonl" > "$OUT/todo-$arm.jsonl"
  [ -s "$OUT/todo-$arm.jsonl" ] || continue
  scp -q -i "$HOME/.ssh/netcortex-ft.pem" "$OUT/todo-$arm.jsonl" "$HOST:ft/todo-$arm.jsonl"
  extra=""; [ "$arm" = qwen-tuned ] && extra="--adapter $ADAPTER"
  "${SSH[@]}" "source /opt/pytorch/bin/activate && python generate.py --prompts ~/ft/todo-$arm.jsonl --out ~/ft/new-$arm.jsonl $extra 2>&1 | grep -E '^\[' | tail -n 3"
  "${SSH[@]}" "cat ~/ft/new-$arm.jsonl" >> "$OUT/pred-$arm.jsonl"
done

"$PY" "$ROOT/finetune/eval_models.py" score "$LINES" "$OUT"/pred-*.jsonl --out "$OUT/results.json" 2>/dev/null
