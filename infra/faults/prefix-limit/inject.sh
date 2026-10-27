#!/bin/bash
# Prefix limit (an experiment): advertise COUNT (default 1001) extra /24s from
# 172.20.0.0/14. A TGW VPN accepts up to 1,000 dynamic routes from the customer
# gateway, and AWS doesn't document what happens past that. Find out.
source "$(dirname "$0")/../lib.sh"
require_bgp
COUNT=${COUNT:-1001}
[ "$COUNT" -le 1024 ] || die "COUNT must be <= 1024 (172.20.0.0/14 has 1024 /24s)"
out=$(router "
if [ \"\$(vtysh -c 'show running-config' | grep -c '^  network 172\.2[0-3]\.')\" -gt 0 ]; then echo already; exit 0; fi
f=/tmp/netcortex-prefix-limit.conf
{
  echo 'ip prefix-list ONPREM seq 10 permit 172.20.0.0/14 le 24'
  echo 'router bgp $(t .onprem_asn)'
  echo ' no bgp network import-check'
  echo ' address-family ipv4 unicast'
  for i in \$(seq 0 \$(($COUNT - 1))); do echo \"  network 172.\$((20 + i / 256)).\$((i % 256)).0/24\"; done
  echo ' exit-address-family'
} > \$f
vtysh -f \$f
echo changed")
report "$out" "advertising $COUNT extra /24s from 172.20.0.0/14"
