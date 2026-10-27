#!/bin/bash
# One tunnel down (partial): the router drops all traffic to and from tunnel 1's AWS
# endpoint and tears down the SA, so it can't renegotiate. Expect TunnelState 0 on
# tunnel 1 only while traffic carries on over tunnel 2 ("is it an outage?").
source "$(dirname "$0")/../lib.sh"
IP=$(t '.vpn_tunnel_outside_ips[0]')
out=$(router "
if iptables -C OUTPUT -d $IP -j DROP 2>/dev/null; then echo already; exit 0; fi
iptables -I OUTPUT -d $IP -j DROP
iptables -I INPUT -s $IP -j DROP
swanctl --terminate --ike aws-t1 --force >/dev/null 2>&1 || true
echo changed")
report "$out" "blocked $IP and terminated aws-t1"
