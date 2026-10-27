#!/bin/bash
# App CPU overload: stress-ng pins every vCPU on app-a. The unit stops itself after
# DURATION seconds (default 1800) in case revert is forgotten.
source "$(dirname "$0")/../lib.sh"
DURATION=${DURATION:-1800}
out=$(ssm "$(t .app_a_instance_id)" "
if systemctl is-active --quiet netcortex-fault-stress; then echo already; exit 0; fi
command -v stress-ng >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -yq stress-ng >/dev/null
systemctl reset-failed netcortex-fault-stress 2>/dev/null || true
systemd-run --quiet --unit netcortex-fault-stress --property RuntimeMaxSec=$DURATION stress-ng --cpu 0 --cpu-load 100
echo changed")
report "$out" "stress-ng --cpu 0 on app-a for up to ${DURATION}s"
