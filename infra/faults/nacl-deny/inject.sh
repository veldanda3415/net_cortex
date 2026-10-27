#!/bin/bash
# NACL deny: rule 50 on the app VPC's network ACL denies on-prem in both directions.
# Expect REJECTs both ways in the app Flow Logs and a CloudTrail CreateNetworkAclEntry.
source "$(dirname "$0")/../lib.sh"
CIDR=$(t .onprem_cidr)
ACL=$(aws ec2 describe-network-acls --filters "Name=vpc-id,Values=$(t .app_vpc_id)" Name=default,Values=true \
  --query 'NetworkAcls[0].NetworkAclId' --output text)
have=$(aws ec2 describe-network-acls --network-acl-ids "$ACL" --query 'length(NetworkAcls[0].Entries[?RuleNumber==`50`])')
if [ "$have" -eq 2 ]; then report already "rule 50 deny $CIDR on $ACL"; exit 0; fi
for dir in --ingress --egress; do
  aws ec2 replace-network-acl-entry --network-acl-id "$ACL" --rule-number 50 $dir --protocol=-1 \
    --cidr-block "$CIDR" --rule-action deny >/dev/null 2>&1 ||
    aws ec2 create-network-acl-entry --network-acl-id "$ACL" --rule-number 50 $dir --protocol=-1 \
      --cidr-block "$CIDR" --rule-action deny >/dev/null
done
report changed "rule 50 deny $CIDR both directions on $ACL"
