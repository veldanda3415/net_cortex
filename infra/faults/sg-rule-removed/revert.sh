#!/bin/bash
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
set_rule_source "HTTP from on-prem clients" 80 "$(t .onprem_cidr)"
