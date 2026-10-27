#!/bin/bash
# MSS clamp removed: flush the router's TCPMSS chain. The spec expected large
# responses to hang, but the AWS VPN side rewrites MSS to 1350 itself (seen in a
# capture on vti1, 2026-10-27), so nothing breaks. Kept as a negative control: an
# RCA should find no impact, not blame the router.
source "$(dirname "$0")/../lib.sh"
out=$(router "
iptables -t mangle -S NETCORTEX-MSS | grep -q TCPMSS || { echo already; exit 0; }
iptables -t mangle -F NETCORTEX-MSS
echo changed")
report "$out" "flushed mangle NETCORTEX-MSS"
