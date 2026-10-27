#!/bin/bash
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
restore_rule "$(t .onprem_cidr)" 80 "HTTP from on-prem clients"
