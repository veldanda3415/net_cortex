#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_bgp
PEER=$(t '.bgp_neighbors[0]')
out=$(router "
vtysh -c 'show running-config' | grep -q '^ neighbor $PEER shutdown' || { echo already; exit 0; }
vtysh -c 'conf t' -c 'router bgp $(t .onprem_asn)' -c 'no neighbor $PEER shutdown'
echo changed")
report "$out" "no neighbor $PEER shutdown"
