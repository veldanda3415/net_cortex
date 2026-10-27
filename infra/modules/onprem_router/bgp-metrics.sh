#!/bin/bash
# Publishes BgpPeerState (1 = Established), PrefixesReceived and PrefixesSent for
# each AWS neighbor (dimension Neighbor). Runs every minute from netcortex-bgp-metrics.timer.
# A neighbor missing from FRR's output (bgpd down, session never configured) reports 0s.
set -euo pipefail

CFG=/etc/netcortex/router.json
REGION=$(jq -r .region "$CFG")
NAMESPACE=$(jq -r .metric_namespace "$CFG")

SUMMARY=$(vtysh -c 'show bgp ipv4 unicast summary json' 2>/dev/null) || SUMMARY='{}'
jq -e . >/dev/null 2>&1 <<<"$SUMMARY" || SUMMARY='{}'

DATA=$(jq -c --argjson s "$SUMMARY" '[
  .tunnels[] | .inside_remote as $n | (($s.peers // {})[$n] // {}) as $p | [{Name: "Neighbor", Value: $n}] as $d |
  { MetricName: "BgpPeerState", Dimensions: $d, Value: (if $p.state == "Established" then 1 else 0 end), Unit: "None" },
  { MetricName: "PrefixesReceived", Dimensions: $d, Value: ($p.pfxRcd // 0), Unit: "Count" },
  { MetricName: "PrefixesSent", Dimensions: $d, Value: ($p.pfxSnt // 0), Unit: "Count" }
]' "$CFG")

/snap/bin/aws cloudwatch put-metric-data --region "$REGION" --namespace "$NAMESPACE" --metric-data "$DATA"
