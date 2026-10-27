#!/bin/bash
source "$(dirname "$0")/../lib.sh"
out=$(ssm "$(t .app_a_instance_id)" "
systemctl is-active --quiet netcortex-fault-stress || { echo already; exit 0; }
systemctl stop netcortex-fault-stress
echo changed")
report "$out" "stopped stress-ng on app-a"
