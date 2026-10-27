#!/bin/bash
# ASN mismatch: the router expects the wrong remote AS on tunnel 1's neighbor.
# Expect a BGP OPEN error (bad peer AS) and a session that never re-establishes.
source "$(dirname "$0")/../lib.sh"
require_bgp
PEER=$(t '.bgp_neighbors[0]')
WRONG=${WRONG_ASN:-64999}
out=$(router "
if vtysh -c 'show running-config' | grep -q '^ neighbor $PEER remote-as $WRONG\$'; then echo already; exit 0; fi
vtysh -c 'conf t' -c 'router bgp $(t .onprem_asn)' -c 'neighbor $PEER remote-as $WRONG'
echo changed")
report "$out" "neighbor $PEER remote-as $WRONG (AWS is $(t .aws_asn))"
