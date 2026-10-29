#!/bin/bash
source "$(dirname "$0")/../lib.sh"
SUBNET=$(t .app_private_subnet_a)
OWN=$(t '.app_private_route_table_ids["private-a"]')
on_own=$(aws ec2 describe-route-tables --route-table-ids "$OWN" \
  --query "length(RouteTables[0].Associations[?SubnetId=='$SUBNET'])")
if [ "$on_own" -gt 0 ]; then report already "$SUBNET already on $OWN"; exit 0; fi
explicit=$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$SUBNET" \
  --query "RouteTables[0].Associations[?SubnetId=='$SUBNET'].RouteTableAssociationId | [0]" --output text)
[ "$explicit" = None ] || aws ec2 disassociate-route-table --association-id "$explicit"
tf_reconcile
report changed "$SUBNET back on $OWN (association recreated by Terraform)"
