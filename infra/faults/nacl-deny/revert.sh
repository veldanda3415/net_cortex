#!/bin/bash
source "$(dirname "$0")/../lib.sh"
ACL=$(aws ec2 describe-network-acls --filters "Name=vpc-id,Values=$(t .app_vpc_id)" Name=default,Values=true \
  --query 'NetworkAcls[0].NetworkAclId' --output text)
have=$(aws ec2 describe-network-acls --network-acl-ids "$ACL" --query 'length(NetworkAcls[0].Entries[?RuleNumber==`50`])')
if [ "$have" -eq 0 ]; then report already "no rule 50 on $ACL"; exit 0; fi
for dir in --ingress --egress; do
  aws ec2 delete-network-acl-entry --network-acl-id "$ACL" --rule-number 50 $dir >/dev/null 2>&1 || true
done
report changed "deleted rule 50 on $ACL"
