#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_bgp
PEER=$(t '.bgp_neighbors[1]')
out=$(router "
vtysh -c 'show running-config' | grep -q '^  neighbor $PEER maximum-prefix' || { echo already; exit 0; }
vtysh -c 'conf t' -c 'router bgp $(t .onprem_asn)' -c 'address-family ipv4 unicast' -c 'no neighbor $PEER maximum-prefix'
vtysh -c 'clear ip bgp $PEER'
echo changed")
report "$out" "no neighbor $PEER maximum-prefix; session cleared"
