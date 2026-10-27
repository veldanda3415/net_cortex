#!/bin/bash
# MSS clamp removed: flush the router's TCPMSS chain. AWS VPN needs a 1399-byte
# tunnel MTU and doesn't do PMTUD, so small requests still work but large responses
# (the onprem-to-nlb-large flow) hang. Expect TGW MTU-exceeded drops.
source "$(dirname "$0")/../lib.sh"
out=$(router "
iptables -t mangle -S NETCORTEX-MSS | grep -q TCPMSS || { echo already; exit 0; }
iptables -t mangle -F NETCORTEX-MSS
echo changed")
report "$out" "flushed mangle NETCORTEX-MSS"
