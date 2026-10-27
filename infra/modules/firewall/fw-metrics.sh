#!/bin/bash
# Publishes ConntrackCount (tracked flows) and ForwardDrops (packets dropped by
# the FORWARD chain since the last run) every minute, dimension Firewall=<name>.
set -euo pipefail

CFG=/etc/netcortex/firewall.json
REGION=$(jq -r .region "$CFG")
NAMESPACE=$(jq -r .metric_namespace "$CFG")
NAME=$(jq -r .name "$CFG")
STATE=/run/netcortex-fw-drops

conntrack=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)
# Packet counter of the final DROP rule in FORWARD.
drops_total=$(iptables -L FORWARD -v -x -n | awk '$3 == "DROP" {n = $1} END {print n + 0}')
drops_prev=$(cat "$STATE" 2>/dev/null || echo "$drops_total")
echo "$drops_total" > "$STATE"
drops=$(( drops_total >= drops_prev ? drops_total - drops_prev : drops_total ))

/snap/bin/aws cloudwatch put-metric-data --region "$REGION" --namespace "$NAMESPACE" --metric-data \
  "[{\"MetricName\":\"ConntrackCount\",\"Dimensions\":[{\"Name\":\"Firewall\",\"Value\":\"$NAME\"}],\"Value\":$conntrack,\"Unit\":\"Count\"},
    {\"MetricName\":\"ForwardDrops\",\"Dimensions\":[{\"Name\":\"Firewall\",\"Value\":\"$NAME\"}],\"Value\":$drops,\"Unit\":\"Count\"}]"
