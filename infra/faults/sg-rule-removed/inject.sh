#!/bin/bash
# SG rule removed: the app web servers' HTTP-from-on-prem rule is narrowed to
# TEST-NET, so on-prem is no longer allowed. NLB health checks
# come from the app VPC, so targets stay healthy while on-prem clients fail (an
# asymmetric symptom). Expect Flow Log REJECTs and a CloudTrail ModifySecurityGroupRules.
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/sg.sh"
set_rule_source "HTTP from on-prem clients" 80 "$NOWHERE"
