#!/bin/bash
# Subnet route table swapped: app private-a is disassociated from its route table and
# associated with the app VPC's main table, which only has the local route. app-a and
# the NLB node in private-a lose their way back to on-prem and the other VPCs.
# Expect CloudTrail DisassociateRouteTable + AssociateRouteTable (neither in the
# catalog: the "uncovered config change" case) and failing flows to app-a.
# Revert can't reuse the association ID, so it lets Terraform recreate it (tf_reconcile).
source "$(dirname "$0")/../lib.sh"
SUBNET=$(t .app_private_subnet_a)
OWN=$(t '.app_private_route_table_ids["private-a"]')
MAIN=$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$(t .app_vpc_id)" Name=association.main,Values=true \
  --query 'RouteTables[0].RouteTableId' --output text)
assoc=$(aws ec2 describe-route-tables --route-table-ids "$OWN" \
  --query "RouteTables[0].Associations[?SubnetId=='$SUBNET'].RouteTableAssociationId | [0]" --output text)
if [ "$assoc" = None ]; then report already "$SUBNET not on $OWN"; exit 0; fi
aws ec2 disassociate-route-table --association-id "$assoc"
aws ec2 associate-route-table --subnet-id "$SUBNET" --route-table-id "$MAIN" >/dev/null
report changed "$SUBNET moved from $OWN to the main route table $MAIN"
