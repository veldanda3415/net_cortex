#!/bin/bash
# FRR max-prefix: the router accepts at most 1 prefix from tunnel 2's AWS peer. The TGW
# advertises more, so FRR logs %MAXPFX, sends a Cease 6/1 and holds the session down.
# Those two lines match no classify rule in the lab config: the "unfamiliar FRR line"
# case. Only the ADJCHANGE Down line is known (bgp_neighbor_down).
source "$(dirname "$0")/../lib.sh"
require_bgp
PEER=$(t '.bgp_neighbors[1]')
MAX=${MAX_PREFIX:-1}
out=$(router "
if vtysh -c 'show running-config' | grep -q '^  neighbor $PEER maximum-prefix $MAX\$'; then echo already; exit 0; fi
vtysh -c 'conf t' -c 'router bgp $(t .onprem_asn)' -c 'address-family ipv4 unicast' -c 'neighbor $PEER maximum-prefix $MAX'
echo changed")
report "$out" "neighbor $PEER maximum-prefix $MAX"
