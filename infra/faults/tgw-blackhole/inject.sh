#!/bin/bash
# TGW blackhole: a static blackhole route in rt-onprem for app private-a (app-a and
# the NLB's AZ-a node). Expect PacketDropCountBlackhole, a CloudTrail
# CreateTransitGatewayRoute, and on-prem flows to app-a failing.
# SearchTransitGatewayRoutes lags by seconds, so trust the create call's answer instead.
source "$(dirname "$0")/../lib.sh"
RT=$(t .tgw_route_table_ids.onprem)
CIDR=$(aws ec2 describe-subnets --subnet-ids "$(t .app_private_subnet_a)" --query 'Subnets[0].CidrBlock' --output text)
if err=$(aws ec2 create-transit-gateway-route --transit-gateway-route-table-id "$RT" \
  --destination-cidr-block "$CIDR" --blackhole 2>&1 >/dev/null); then
  report changed "$CIDR blackhole in rt-onprem $RT"
elif [[ $err == *RouteAlreadyExists* ]]; then
  report already "$CIDR already in $RT"
else
  die "$err"
fi
