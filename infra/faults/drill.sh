#!/bin/bash
# drill.sh <fault> [max_wait_seconds]: inject a fault, time how long the lab's own
# CloudWatch alarms take to notice, revert it, and time the recovery. Prints one
# JSON line. The alarm times are the floor for what NetCortex can detect from metrics.
#
#   SETTLE   seconds to keep watching for more alarms after the first (default 120)
#   COOLDOWN seconds to keep watching after revert for late alarms (default 180)
#   RECOVER  max seconds to wait for every alarm to clear after revert (default 900)
#
# With NetCortex running (`serve` plus `alarm-bridge`, see infra/faults/README.md), the
# drill also holds the fault until NetCortex's RCA arrives and scores it against the
# faults log (score.py, expect.json); the result is the `netcortex` key.
#   SCORE          1 to score NetCortex (default 1 if NETCORTEX_URL/health answers)
#   NETCORTEX_URL  NetCortex webhook base URL (default http://127.0.0.1:8000)
#   BRIDGE_OUT     where the alarm bridge saves incidents (default <repo>/output/alarm_bridge)
#   RCA_WAIT       max seconds to wait for the alarm-triggered RCA (default 420)
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

NETCORTEX_URL=${NETCORTEX_URL:-http://127.0.0.1:8000}
BRIDGE_OUT=${BRIDGE_OUT:-$DIR/../../output/alarm_bridge}
if [ -z "${SCORE:-}" ]; then
  if curl -sf -m 5 "$NETCORTEX_URL/health" >/dev/null 2>&1; then SCORE=1; else
    SCORE=0
    echo "NetCortex not reachable at $NETCORTEX_URL: drilling alarms only" >&2
  fi
fi

# find_record: the newest saved incident whose window ends after the injection.
find_record() {
  local f
  for f in $(ls -t "$BRIDGE_OUT"/*.json 2>/dev/null); do
    [ "$(date -d "$(jq -r .incident.reported_at "$f")" +%s)" -ge "$injected" ] && { echo "$f"; return 0; }
  done
  return 1
}

# manual_incident: what an on-call engineer would file when users complain but no
# alarm fired. Says nothing about the fault. Prints the saved record's path.
manual_incident() {
  local payload resp out auth=()
  payload=$(jq -cn --arg r "$AWS_REGION" --arg at "$(date -u +%FT%TZ)" '{
    description: "Users report intermittent problems reaching the app from on-prem and between VPCs. No alarm has fired.",
    region: $r, severity: "high", source_system: "drill", reported_at: $at}')
  [ -z "${NETCORTEX_WEBHOOK_TOKEN:-}" ] || auth=(-H "Authorization: Bearer $NETCORTEX_WEBHOOK_TOKEN")
  resp=$(curl -sf -m 600 "${auth[@]}" -H 'Content-Type: application/json' -d "$payload" "$NETCORTEX_URL/incidents") || return 1
  mkdir -p "$BRIDGE_OUT"
  out="$BRIDGE_OUT/drill-$FAULT-$(date +%s).json"
  jq -n --argjson i "$payload" --argjson r "$resp" '{incident: $i, report: $r, posted_at: (now | todate)}' >"$out"
  echo "$out"
}

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

# NetCortex: wait for the RCA the alarm bridge triggers, or ask for one if no alarm
# fired. Either way it runs while the fault is still in place, since some of its
# evidence (the TGW route snapshot) is only current state.
record=""
trigger=""
if [ "$SCORE" = 1 ]; then
  if [ -n "$seen" ]; then
    echo "waiting up to ${RCA_WAIT:-420}s for NetCortex's RCA via the alarm bridge" >&2
    for _ in $(seq 1 $((${RCA_WAIT:-420} / 15))); do
      record=$(find_record) && [ -n "$record" ] && trigger=alarm && break
      sleep 15
    done
  fi
  if [ -z "$record" ]; then
    echo "no alarm-triggered RCA; posting a manual incident" >&2
    record=$(manual_incident) && trigger=manual || { echo "warning: manual incident failed" >&2; record=""; }
  fi
fi

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

# Score the RCA against the faults log, the ground truth. The drill may read it;
# NetCortex may not.
netcortex=null
if [ -n "$record" ]; then
  truth=$(mktemp)
  aws logs filter-log-events --log-group-name "$(jq -r .log_group "$FAULT_TARGETS_FILE")" \
    --log-stream-names "$FAULT" --start-time $(((injected - 300) * 1000)) \
    --query 'events[].message' --output json | jq -c '.[] | fromjson' >"$truth"
  netcortex=$(python3 "$DIR/score.py" "$FAULT" "$truth" "$record" "$trigger") || netcortex=null
  rm -f "$truth"
fi

jq -cn --arg f "$FAULT" --arg at "$(date -u -d "@$injected" +%FT%TZ)" --argjson a "$alarms" \
  --argjson held "$((reverted - injected))" --argjson r "$recover" --argjson nc "$netcortex" \
  '{fault: $f, injected_at: $at, detect_seconds: ($a[0].seconds // null), alarms: $a,
    held_seconds: $held, recover_seconds: $r, netcortex: $nc}'
