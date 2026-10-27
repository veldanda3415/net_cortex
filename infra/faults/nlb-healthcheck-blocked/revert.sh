#!/bin/bash
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
set_rule_source "HTTP health checks from the NLB nodes in the app VPC" 80 "$(t .app_cidr)"
