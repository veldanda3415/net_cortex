#!/bin/bash
# NLB health check blocked: the app web servers' HTTP-from-the-app-VPC rule (the NLB
# nodes' health checks) is narrowed to TEST-NET. Client traffic keeps its source IP and is still allowed.
# Expect UnHealthyHostCount 2 and a CloudTrail ModifySecurityGroupRules.
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
set_rule_source "HTTP health checks from the NLB nodes in the app VPC" 80 "$NOWHERE"
