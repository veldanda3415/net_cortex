# Shared helpers for infra/faults/<name>/{inject,revert}.sh. Source it; don't run it.
#
# Every script is idempotent: it checks the current state first, so running inject
# twice (or revert on a healthy lab) is a no-op that still logs what it found.
# Each run writes one JSON event to the faults log group, the ground truth for evals.
# NetCortex must never read that log group.
set -euo pipefail
shopt -s inherit_errexit

export AWS_PROFILE=${AWS_PROFILE:-aws-veldanda-admin}
export AWS_PAGER=""

FAULTS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAB_DIR="$FAULTS_DIR/../live/lab"
FAULT=$(basename "$(cd "$(dirname "$0")" && pwd)")
ACTION=$(basename "$0" .sh)

# The lab's IDs come from one Terraform output. FAULT_TARGETS_FILE lets drill.sh (or
# you) read it once instead of on every script.
if [ -n "${FAULT_TARGETS_FILE:-}" ]; then
  TARGETS=$(cat "$FAULT_TARGETS_FILE")
else
  TARGETS=$(terraform -chdir="$LAB_DIR" output -json fault_targets)
fi
t() { jq -r "$1" <<<"$TARGETS"; }
REGION=$(t .region 2>/dev/null || true)
[ -n "$REGION" ] && [ "$REGION" != null ] || { echo "no fault_targets: is the lab up and AWS_PROFILE valid?" >&2; exit 1; }
export AWS_REGION=$REGION

say() { echo "[$FAULT/$ACTION] $*" >&2; }
die() { say "ERROR: $*"; exit 1; }

# Skip (exit 2) when the fault doesn't apply to the lab's current toggles.
skip() { say "skipped: $*"; log_fault skipped "$*"; exit 2; }
require_bgp() { [ "$(t .vpn_mode)" != static ] || skip "needs vpn_mode bgp_*"; }
require_inspection() { [ "$(t .enable_inspection)" = true ] || skip "needs enable_inspection=true"; }
require_no_inspection() { [ "$(t .enable_inspection)" = false ] || skip "needs enable_inspection=false"; }
require_peering() { [ "$(t .enable_peering)" = true ] || skip "needs enable_peering=true"; }

# log_fault <result> <detail>: result is injected | reverted | already | skipped.
log_fault() {
  local group stream now msg
  group=$(t .log_group)
  stream=$FAULT
  now=$(date -u +%s%3N)
  msg=$(jq -cn --arg fault "$FAULT" --arg action "$ACTION" --arg result "$1" \
    --arg detail "$2" --arg at "$(date -u +%FT%T.%3NZ)" --arg by "$(whoami)" \
    '{fault: $fault, action: $action, result: $result, detail: $detail, at: $at, by: $by}')
  aws logs create-log-stream --log-group-name "$group" --log-stream-name "$stream" 2>/dev/null || true
  aws logs put-log-events --log-group-name "$group" --log-stream-name "$stream" \
    --log-events "$(jq -cn --argjson ts "$now" --arg m "$msg" '[{timestamp: $ts, message: $m}]')" >/dev/null
  echo "$msg"
}

# ssm <instance-id> <script>: run a shell script as root via SSM Run Command, print
# its stdout, fail if it fails. Scripts run under bash with set -e.
ssm() {
  local id=$1 script=$2 cmd status
  cmd=$(aws ssm send-command --instance-ids "$id" --document-name AWS-RunShellScript \
    --comment "netcortex fault $FAULT/$ACTION" \
    --parameters "$(jq -cn --arg s "set -e
$script" '{commands: [$s], executionTimeout: ["600"]}')" \
    --query Command.CommandId --output text)
  for _ in $(seq 1 120); do
    status=$(aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
      --query Status --output text 2>/dev/null || echo Pending)
    case $status in
      Pending | InProgress | Delayed) sleep 3 ;;
      *) break ;;
    esac
  done
  aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query StandardOutputContent --output text
  if [ "$status" != Success ]; then
    aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
      --query StandardErrorContent --output text >&2
    die "SSM command on $id ended $status"
  fi
}

router() { ssm "$(t .router_instance_id)" "$1"; }

# report <changed|already> <detail>: log the outcome of this run. The first argument
# is usually the last line a remote script printed.
report() {
  local state=${1##*$'\n'} result
  if [ "$state" = changed ]; then
    result=$([ "$ACTION" = inject ] && echo injected || echo reverted)
  else
    result=already
  fi
  log_fault "$result" "$2"
}
