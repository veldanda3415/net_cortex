#!/bin/bash
# BGP neighbor shutdown: the router administratively shuts its session to tunnel 1's
# AWS peer. Expect BGP Idle in the BGP log, BgpPeerState 0 for that neighbor, a
# Network Manager BGP event, and traffic failing over to tunnel 2.
source "$(dirname "$0")/../lib.sh"
require_bgp
PEER=$(t '.bgp_neighbors[0]')
out=$(router "
if vtysh -c 'show running-config' | grep -q '^ neighbor $PEER shutdown'; then echo already; exit 0; fi
vtysh -c 'conf t' -c 'router bgp $(t .onprem_asn)' -c 'neighbor $PEER shutdown'
echo changed")
report "$out" "neighbor $PEER shutdown"
