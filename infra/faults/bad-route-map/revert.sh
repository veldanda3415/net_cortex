#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_bgp
out=$(router "
vtysh -c 'show running-config' | grep -q '^route-map AWS-OUT-T1 deny 5' || { echo already; exit 0; }
for n in 1 2; do vtysh -c 'conf t' -c \"no route-map AWS-OUT-T\$n deny 5\"; done
vtysh -c 'clear bgp * soft out'
echo changed")
report "$out" "removed route-map AWS-OUT-T1/T2 deny 5"
