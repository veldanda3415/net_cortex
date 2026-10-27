#!/bin/bash
# Peering route removed: delete app -> partner from app's private route tables. The
# app-to-partner flow fails while on-prem is unaffected. Expect a CloudTrail DeleteRoute.
source "$(dirname "$0")/../lib.sh"
require_peering
CIDR=$(t .partner_cidr)
changed=already
for rt in $(t '.app_private_route_table_ids[]'); do
  have=$(aws ec2 describe-route-tables --route-table-ids "$rt" --query "length(RouteTables[0].Routes[?DestinationCidrBlock=='$CIDR'])")
  [ "$have" -eq 0 ] && continue
  aws ec2 delete-route --route-table-id "$rt" --destination-cidr-block "$CIDR"
  changed=changed
done
report "$changed" "$CIDR route removed from app private route tables"
