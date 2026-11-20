#!/bin/bash
# campaign.sh <round> [fault ...]: run drill.sh over many faults, one at a time, and
# append one JSON line per drill to a campaign file. Built for unattended overnight
# runs that collect fine-tuning data: recordings (telemetry.record_dir), RCAs
# (output/alarm_bridge) and the score against the faults log, per fault.
#
#   round   a label stored on every line, e.g. r1 (train) or r2 (test)
#   faults  default: every fault folder, in a fixed order
#
#   OUT        campaign file (default <repo>/output/campaign/campaign-<round>.jsonl)
#   GAP        seconds to wait after a confirmed recovery before the next fault
#              (default 480: README "Gaps between drills", 8 minutes in the lab)
#   EXTRA_WAIT seconds to keep waiting for recovery when drill.sh gave up on it
#              (default 900). Still unhealthy after that: the campaign STOPS, because
#              a fault drilled into an unrecovered lab gives an ambiguous answer key.
#   DRILL_TIMEOUT  hard cap per drill in seconds (default 3600)
#
# Resumable: a fault already drilled for this round in OUT (not an error, not skipped
# for lab toggles) is not drilled again, so after a crash or a stop, or after turning
# inspection on for appliance-mode-off and firewall-stopped, run the same command again.
#
#   cd infra && nohup faults/campaign.sh r1 > campaign-r1.log 2>&1 &
set -uo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aws-veldanda-admin} AWS_PAGER=""
DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/../.." && pwd)
ROUND=${1:?usage: campaign.sh <round> [fault ...]}
shift
GAP=${GAP:-480}
EXTRA_WAIT=${EXTRA_WAIT:-900}
DRILL_TIMEOUT=${DRILL_TIMEOUT:-3600}
OUT=${OUT:-$ROOT/output/campaign/campaign-$ROUND.jsonl}
mkdir -p "$(dirname "$OUT")"
touch "$OUT"

log() { echo "[campaign $ROUND $(date -u +%FT%TZ)] $*" >&2; }

if [ $# -gt 0 ]; then
  FAULTS=("$@")
else
  FAULTS=()
  for d in "$DIR"/*/; do
    [ -x "$d/inject.sh" ] && FAULTS+=("$(basename "$d")")
  done
fi

# Read the lab's IDs once for every drill and script.
FAULT_TARGETS_FILE=$(mktemp)
trap 'rm -f "$FAULT_TARGETS_FILE"' EXIT
terraform -chdir="$DIR/../live/lab" output -json fault_targets >"$FAULT_TARGETS_FILE" \
  || { log "no fault_targets: is the lab up?"; exit 1; }
export FAULT_TARGETS_FILE
AWS_REGION=$(jq -r .region "$FAULT_TARGETS_FILE")
export AWS_REGION

healthy_since() {  # healthy_since <epoch>: lab_health.py exit status
  "${PYTHON:-python3}" "$DIR/lab_health.py" --since "$1" --region "$AWS_REGION" >/dev/null 2>&1
}

wait_healthy() {  # wait_healthy <since> <max seconds>: 0 once healthy, 1 on timeout
  local since=$1 until=$(($(date +%s) + $2))
  while [ "$(date +%s)" -lt "$until" ]; do
    healthy_since "$since" && return 0
    sleep 30
  done
  return 1
}

done_already() {  # a drilled line (not error, not skipped) for this fault and round
  jq -e -s --arg f "$1" --arg r "$ROUND" \
    'any(.[]; .fault == $f and .round == $r and .result != "error" and .result != "skipped")' "$OUT" >/dev/null 2>&1
}

log "faults: ${FAULTS[*]}"
log "output: $OUT"
if ! healthy_since $(($(date +%s) - 300)); then
  log "lab not healthy before the first drill; waiting up to ${EXTRA_WAIT}s"
  wait_healthy $(($(date +%s) - 300)) "$EXTRA_WAIT" || { log "lab still unhealthy: not starting"; exit 1; }
fi

n=0
for fault in "${FAULTS[@]}"; do
  n=$((n + 1))
  if done_already "$fault"; then
    log "($n/${#FAULTS[@]}) $fault: already in $OUT, skipping"
    continue
  fi
  log "($n/${#FAULTS[@]}) $fault: drilling"
  started=$(date +%s)
  line=$(timeout "$DRILL_TIMEOUT" "$DIR/drill.sh" "$fault")
  rc=$?

  if [ $rc -ne 0 ] || ! jq -e . <<<"$line" >/dev/null 2>&1; then
    log "$fault: drill.sh failed (rc=$rc); reverting to be safe"
    "$DIR/$fault/revert.sh" >&2 || log "$fault: revert also failed"
    jq -cn --arg f "$fault" --arg r "$ROUND" --argjson rc "$rc" --arg at "$(date -u -d "@$started" +%FT%TZ)" \
      '{fault: $f, round: $r, result: "error", rc: $rc, started_at: $at}' >>"$OUT"
    reverted=$(date +%s)
    if ! wait_healthy "$reverted" "$EXTRA_WAIT"; then
      log "$fault: lab not healthy after a failed drill; stopping the campaign"
      exit 1
    fi
    sleep "$GAP"
    continue
  fi

  jq -c --arg r "$ROUND" '. + {round: $r}' <<<"$line" >>"$OUT"

  if [ "$(jq -r '.result // empty' <<<"$line")" = skipped ]; then
    log "$fault: skipped (lab toggles don't fit this fault)"
    continue
  fi

  verdict=$(jq -r '.netcortex.verdict // "no-score"' <<<"$line")
  log "$fault: done, netcortex verdict=$verdict"

  rec=$(jq -r '.recovered_at // empty' <<<"$line")
  if [ -z "$rec" ]; then
    log "$fault: drill.sh did not confirm recovery; waiting up to ${EXTRA_WAIT}s more"
    since=$(date -d "$(jq -r .reverted_at <<<"$line")" +%s)
    if ! wait_healthy "$since" "$EXTRA_WAIT"; then
      log "$fault: lab still unhealthy; stopping the campaign so the next answer key stays clean"
      exit 1
    fi
    rec_epoch=$(date +%s)
  else
    rec_epoch=$(date -d "$rec" +%s)
  fi
  wait_for=$((rec_epoch + GAP - $(date +%s)))
  if [ "$wait_for" -gt 0 ]; then
    log "gap: ${wait_for}s before the next fault"
    sleep "$wait_for"
  fi
done

log "campaign $ROUND finished: $(jq -s 'length' "$OUT") lines in $OUT"
jq -s -r 'group_by(.netcortex.verdict // .result // "no-score")
  | map("\(.[0].netcortex.verdict // .[0].result // "no-score"): \(length)") | .[]' "$OUT" >&2
