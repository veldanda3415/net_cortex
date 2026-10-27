#!/bin/bash
source "$(dirname "$0")/../lib.sh"
IP=$(t '.vpn_tunnel_outside_ips[0]')
out=$(router "
iptables -C OUTPUT -d $IP -j DROP 2>/dev/null || { echo already; exit 0; }
while iptables -D OUTPUT -d $IP -j DROP 2>/dev/null; do :; done
while iptables -D INPUT -s $IP -j DROP 2>/dev/null; do :; done
swanctl --initiate --child aws-t1 --timeout 30 >/dev/null 2>&1 || true
echo changed")
report "$out" "unblocked $IP and re-initiated aws-t1"
