#!/bin/bash
# drill.sh <fault> [max_wait_seconds]: inject a fault, time how long the lab's own
# CloudWatch alarms take to notice, revert it, and time the recovery. Prints one
# JSON line. The alarm times are the floor for what NetCortex can detect from metrics.
#
#   SETTLE   seconds to keep watching for more alarms after the first (default 120)
#   COOLDOWN seconds to keep watching after revert for late alarms (default 180)
#   RECOVER  max seconds to wait for every alarm to clear after revert (default 900)
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aws-veldanda-admin} AWS_PAGER=""
DIR=$(cd "$(dirname "$0")" && pwd)
FAULT=${1:?usage: drill.sh <fault> [max_wait_seconds]}
WAIT=${2:-480}
[ -x "$DIR/$FAULT/inject.sh" ] || { echo "unknown fault: $FAULT" >&2; exit 1; }

if [ -z "${FAULT_TARGETS_FILE:-}" ]; then
  FAULT_TARGETS_FILE=$(mktemp)
  terraform -chdir="$DIR/../live/lab" output -json fault_targets >"$FAULT_TARGETS_FILE"
  export FAULT_TARGETS_FILE
fi
export AWS_REGION=$(jq -r .region "$FAULT_TARGETS_FILE")

alarming() {
  aws cloudwatch describe-alarms --alarm-name-prefix netcortex-lab- --state-value ALARM \
    --query 'MetricAlarms[].AlarmName' --output text | tr '\t' '\n' | grep -v '^$' | sort || true
}
before=$(alarming)
[ -z "$before" ] || echo "warning: already alarming: $(echo $before)" >&2

rc=0
"$DIR/$FAULT/inject.sh" >&2 || rc=$?
if [ $rc -eq 2 ]; then jq -cn --arg f "$FAULT" '{fault: $f, result: "skipped"}'; exit 0; fi
[ $rc -eq 0 ] || exit $rc
injected=$(date +%s)

# Watch for alarms that weren't already firing.
seen=""
watch() {
  local now
  now=$(comm -13 <(echo "$before") <(alarming) | grep -v '^$' || true)
  seen=$(printf '%s\n%s\n' "$seen" "$now" | grep -v '^$' | sort -u || true)
}
deadline=$((injected + WAIT))
while [ "$(date +%s)" -lt "$deadline" ]; do
  had=$seen
  watch
  [ -n "$had" ] || [ -z "$seen" ] || deadline=$(($(date +%s) + ${SETTLE:-120}))
  sleep 15
done

"$DIR/$FAULT/revert.sh" >&2
reverted=$(date +%s)

# still_alarming: true while any alarm this fault raised is in ALARM; records when.
cleared=$(date +%s)
still_alarming() {
  [ -n "$seen" ] && [ -n "$(comm -12 <(echo "$seen") <(alarming))" ] && cleared=$(date +%s)
}

# Some metrics (the NLB's especially) land minutes late, so keep watching after the
# revert; an alarm that fires then still belongs to this fault, not the next one.
cooldown_end=$((reverted + ${COOLDOWN:-180}))
while [ "$(date +%s)" -lt "$cooldown_end" ]; do
  watch
  still_alarming || true
  sleep 15
done

# Alarm timestamps are exact; the polling above only decides how long to watch.
alarms='[]'
for name in $seen; do
  ts=$(aws cloudwatch describe-alarm-history --alarm-name "$name" --history-item-type StateUpdate \
    --start-date "$(date -u -d "@$injected" +%FT%TZ)" \
    --query "AlarmHistoryItems[?contains(HistorySummary, 'to ALARM')] | [-1].Timestamp" --output text)
  [ "$ts" != None ] || continue
  alarms=$(jq -c --arg n "${name#netcortex-lab-}" --argjson s "$(($(date -d "$ts" +%s) - injected))" \
    --argjson h "$((reverted - injected))" \
    '. + [{alarm: $n, seconds: $s, after_revert: ($s > $h)}] | sort_by(.seconds)' <<<"$alarms")
done

recover=null
if [ -n "$seen" ]; then
  for _ in $(seq 1 $((${RECOVER:-900} / 15))); do
    still_alarming || { recover=$((cleared - reverted)); break; }
    sleep 15
  done
fi

jq -cn --arg f "$FAULT" --arg at "$(date -u -d "@$injected" +%FT%TZ)" --argjson a "$alarms" \
  --argjson held "$((reverted - injected))" --argjson r "$recover" \
  '{fault: $f, injected_at: $at, detect_seconds: ($a[0].seconds // null), alarms: $a,
    held_seconds: $held, recover_seconds: $r}'
