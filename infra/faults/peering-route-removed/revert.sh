#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_peering
CIDR=$(t .partner_cidr)
PCX=$(t .peering_connection_id)
changed=already
for rt in $(t '.app_private_route_table_ids[]'); do
  have=$(aws ec2 describe-route-tables --route-table-ids "$rt" --query "length(RouteTables[0].Routes[?DestinationCidrBlock=='$CIDR'])")
  [ "$have" -gt 0 ] && continue
  aws ec2 create-route --route-table-id "$rt" --destination-cidr-block "$CIDR" --vpc-peering-connection-id "$PCX" >/dev/null
  changed=changed
done
report "$changed" "$CIDR -> $PCX restored in app private route tables"
