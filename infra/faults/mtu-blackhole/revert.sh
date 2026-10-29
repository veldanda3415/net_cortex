#!/bin/bash
source "$(dirname "$0")/../lib.sh"
out=$(router "
iptables -S NETCORTEX-MTU >/dev/null 2>&1 || { echo already; exit 0; }
iptables -D FORWARD -o vti+ -j NETCORTEX-MTU 2>/dev/null || true
iptables -D FORWARD -i vti+ -j NETCORTEX-MTU 2>/dev/null || true
iptables -F NETCORTEX-MTU
iptables -X NETCORTEX-MTU
echo changed")
report "$out" "removed NETCORTEX-MTU"
