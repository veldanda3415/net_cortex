#!/bin/bash
# NAT gateway deleted: the security VPC's only NAT gateway is deleted (its EIP stays).
# Spoke egress dies, as with nat-route-removed, but the cause is DeleteNatGateway,
# which the catalog doesn't know, and the deleted gateway is gone from the topology.
# Revert lets Terraform create a new one (new ID) and repoint the routes and alarms.
source "$(dirname "$0")/../lib.sh"
require_no_inspection
NAT=$(t .nat_gateway_id)
state=$(aws ec2 describe-nat-gateways --nat-gateway-ids "$NAT" --query 'NatGateways[0].State' --output text 2>/dev/null || echo deleted)
case $state in deleted | deleting) report already "$NAT is $state"; exit 0 ;; esac
aws ec2 delete-nat-gateway --nat-gateway-id "$NAT" >/dev/null
aws ec2 wait nat-gateway-deleted --nat-gateway-ids "$NAT"
report changed "deleted NAT gateway $NAT"
