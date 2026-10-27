#!/bin/bash
# NLB health check blocked: revoke HTTP from the app VPC (the NLB nodes) on the app
# web servers. Client traffic keeps its source IP and is still allowed.
# Expect UnHealthyHostCount 2 and a CloudTrail RevokeSecurityGroupIngress.
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
revoke_rule "$(t .app_cidr)" 80
