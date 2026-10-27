#!/bin/bash
# TGW propagation removed: the app attachment stops propagating into rt-onprem, so
# on-prem has no route to 10.10/16. Expect the route gone, NoRoute drops and a
# CloudTrail DisableTransitGatewayRouteTablePropagation.
source "$(dirname "$0")/../lib.sh"
require_no_inspection
RT=$(t .tgw_route_table_ids.onprem)
ATT=$(t .app_attachment_id)
state=$(aws ec2 get-transit-gateway-route-table-propagations --transit-gateway-route-table-id "$RT" \
  --filters "Name=transit-gateway-attachment-id,Values=$ATT" --query 'TransitGatewayRouteTablePropagations[0].State' --output text)
if [ "$state" != enabled ]; then report already "app propagation into $RT is $state"; exit 0; fi
aws ec2 disable-transit-gateway-route-table-propagation --transit-gateway-route-table-id "$RT" \
  --transit-gateway-attachment-id "$ATT" >/dev/null
report changed "disabled app ($ATT) propagation into rt-onprem $RT"
