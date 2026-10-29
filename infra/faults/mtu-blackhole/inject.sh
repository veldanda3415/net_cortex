#!/bin/bash
# MTU black hole: the router silently drops packets over 1200 bytes in both directions
# on the tunnels, with no ICMP. Small requests still work; full-size TCP segments
# (MSS 1350 on AWS VPN) vanish, so large transfers stall. No config change on AWS and
# no log line anywhere: the honest "no cause verified" case. LEN overrides the limit.
source "$(dirname "$0")/../lib.sh"
LEN=${LEN:-1200}
out=$(router "
if iptables -S NETCORTEX-MTU 2>/dev/null | grep -q DROP; then echo already; exit 0; fi
iptables -N NETCORTEX-MTU 2>/dev/null || true
iptables -A NETCORTEX-MTU -m length --length $LEN:65535 -j DROP
iptables -C FORWARD -o vti+ -j NETCORTEX-MTU 2>/dev/null || iptables -I FORWARD 1 -o vti+ -j NETCORTEX-MTU
iptables -C FORWARD -i vti+ -j NETCORTEX-MTU 2>/dev/null || iptables -I FORWARD 1 -i vti+ -j NETCORTEX-MTU
echo changed")
report "$out" "drop packets >= $LEN bytes on vti+ (no ICMP)"
