#!/bin/bash
# NAT route removed: delete 0/0 -> NAT from the security VPC's TGW subnets. Spoke
# egress dies, so their SSM agents and CloudWatch publishing go offline too (the
# observability blind spot). Expect NoRoute or REJECTs and a CloudTrail DeleteRoute.
source "$(dirname "$0")/../lib.sh"
require_no_inspection
changed=already
for rt in $(t '.security_tgw_route_table_ids[]'); do
  nat=$(aws ec2 describe-route-tables --route-table-ids "$rt" \
    --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].NatGatewayId | [0]" --output text)
  [ "$nat" = None ] && continue
  aws ec2 delete-route --route-table-id "$rt" --destination-cidr-block 0.0.0.0/0
  changed=changed
done
report "$changed" "0/0 -> NAT removed from the security TGW subnet route tables"
