#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_no_inspection
NAT=$(t .nat_gateway_id)
state=$(aws ec2 describe-nat-gateways --nat-gateway-ids "$NAT" --query 'NatGateways[0].State' --output text 2>/dev/null || echo deleted)
if [ "$state" = available ]; then report already "$NAT is available"; exit 0; fi
tf_reconcile
report changed "NAT gateway recreated by Terraform as $(t .nat_gateway_id) (was $NAT)"
