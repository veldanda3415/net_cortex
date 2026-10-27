#!/bin/bash
source "$(dirname "$0")/../lib.sh"
RT=$(t .tgw_route_table_ids.onprem)
CIDR=$(aws ec2 describe-subnets --subnet-ids "$(t .app_private_subnet_a)" --query 'Subnets[0].CidrBlock' --output text)
if err=$(aws ec2 delete-transit-gateway-route --transit-gateway-route-table-id "$RT" \
  --destination-cidr-block "$CIDR" 2>&1 >/dev/null); then
  report changed "deleted $CIDR blackhole from rt-onprem $RT"
elif [[ $err == *NotFound* ]]; then
  report already "no $CIDR static route in $RT"
else
  die "$err"
fi
