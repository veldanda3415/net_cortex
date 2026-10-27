#!/bin/bash
# SG rule removed: revoke HTTP from on-prem on the app web servers. NLB health checks
# come from the app VPC, so targets stay healthy while on-prem clients fail (an
# asymmetric symptom). Expect Flow Log REJECTs and a CloudTrail RevokeSecurityGroupIngress.
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
revoke_rule "$(t .onprem_cidr)" 80
