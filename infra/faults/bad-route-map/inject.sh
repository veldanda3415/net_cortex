#!/bin/bash
# Bad route-map: a deny entry ahead of the permit stops 172.16/16 being advertised on
# both tunnels. Expect sessions still Established but PrefixesSent 0, the on-prem
# route uninstalled from the TGW, and on-prem flows failing with no route.
source "$(dirname "$0")/../lib.sh"
require_bgp
out=$(router "
if vtysh -c 'show running-config' | grep -q '^route-map AWS-OUT-T1 deny 5'; then echo already; exit 0; fi
for n in 1 2; do
  vtysh -c 'conf t' -c \"route-map AWS-OUT-T\$n deny 5\" -c 'match ip address prefix-list ONPREM'
done
vtysh -c 'clear bgp * soft out'
echo changed")
report "$out" "route-map AWS-OUT-T1/T2 deny 5 match ONPREM"
