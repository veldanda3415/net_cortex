#!/bin/bash
# Loss/latency: tc netem on both tunnel interfaces. No control-plane change at all,
# a "no config culprit" test. DELAY and LOSS override the defaults.
source "$(dirname "$0")/../lib.sh"
DELAY=${DELAY:-150ms}
LOSS=${LOSS:-5%}
out=$(router "
if tc qdisc show dev vti1 | grep -q netem; then echo already; exit 0; fi
for v in vti1 vti2; do tc qdisc replace dev \$v root netem delay $DELAY loss $LOSS; done
echo changed")
report "$out" "netem delay $DELAY loss $LOSS on vti1, vti2"
