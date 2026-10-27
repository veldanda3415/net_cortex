#!/bin/bash
source "$(dirname "$0")/../lib.sh"
out=$(router "
tc qdisc show dev vti1 | grep -q netem || tc qdisc show dev vti2 | grep -q netem || { echo already; exit 0; }
for v in vti1 vti2; do tc qdisc del dev \$v root 2>/dev/null || true; done
echo changed")
report "$out" "removed netem"
