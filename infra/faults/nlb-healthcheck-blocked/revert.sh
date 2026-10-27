#!/bin/bash
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
restore_rule "$(t .app_cidr)" 80 "HTTP health checks from the NLB nodes in the app VPC"
