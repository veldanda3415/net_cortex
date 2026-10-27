#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_bgp
PEER=$(t '.bgp_neighbors[0]')
ASN=$(t .aws_asn)
out=$(router "
if vtysh -c 'show running-config' | grep -q '^ neighbor $PEER remote-as $ASN\$'; then echo already; exit 0; fi
vtysh -c 'conf t' -c 'router bgp $(t .onprem_asn)' -c 'neighbor $PEER remote-as $ASN'
echo changed")
report "$out" "neighbor $PEER remote-as $ASN"
