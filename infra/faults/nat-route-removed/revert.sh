#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_no_inspection
NAT=$(t .nat_gateway_id)
changed=already
for rt in $(t '.security_tgw_route_table_ids[]'); do
  have=$(aws ec2 describe-route-tables --route-table-ids "$rt" \
    --query "length(RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'])")
  [ "$have" -gt 0 ] && continue
  aws ec2 create-route --route-table-id "$rt" --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "$NAT" >/dev/null
  changed=changed
done
report "$changed" "0/0 -> $NAT restored in the security TGW subnet route tables"
