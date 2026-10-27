#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_bgp
out=$(router "
nets=\$(vtysh -c 'show running-config' | grep '^  network 172\.2[0-3]\.' || true)
[ -n \"\$nets\" ] || { echo already; exit 0; }
f=/tmp/netcortex-prefix-limit-revert.conf
{
  echo 'router bgp $(t .onprem_asn)'
  echo ' address-family ipv4 unicast'
  echo \"\$nets\" | sed 's/^  network/  no network/'
  echo ' exit-address-family'
  echo ' bgp network import-check'
  echo 'no ip prefix-list ONPREM seq 10 permit 172.20.0.0/14 le 24'
} > \$f
vtysh -f \$f
echo changed")
report "$out" "withdrew the extra /24s"
