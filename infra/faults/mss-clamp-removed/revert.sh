#!/bin/bash
source "$(dirname "$0")/../lib.sh"
# Same rules as router-up.sh.
out=$(router "
if iptables -t mangle -S NETCORTEX-MSS | grep -q TCPMSS; then echo already; exit 0; fi
MSS=\$(jq -r .mss /etc/netcortex/router.json)
iptables -t mangle -A NETCORTEX-MSS -o vti+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss \$MSS
iptables -t mangle -A NETCORTEX-MSS -i vti+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss \$MSS
echo changed")
report "$out" "restored the MSS clamp"
