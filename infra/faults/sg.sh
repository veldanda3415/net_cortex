# Security group rule helpers for the app web servers. Source after lib.sh.
#
# A fault narrows an existing rule's source to TEST-NET (192.0.2.0/24) instead of
# revoking it. The rule keeps its ID, so after revert Terraform sees no drift; a
# revoke-and-reauthorize would leave a new ID and break the next apply.
SG=$(t .app_web_sg_id)
NOWHERE=192.0.2.0/24

# rule_field <description> <field>: the inbound rule Terraform created with that description.
rule_field() {
  aws ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG" \
    --query "SecurityGroupRules[?!IsEgress && Description=='$1'] | [0].$2" --output text
}

# set_rule_source <description> <port> <cidr>
set_rule_source() {
  local id cidr
  id=$(rule_field "$1" SecurityGroupRuleId)
  [ "$id" != None ] || die "no rule \"$1\" on $SG"
  cidr=$(rule_field "$1" CidrIpv4)
  if [ "$cidr" = "$3" ]; then report already "$id ($1) already allows tcp/$2 from $3"; exit 0; fi
  aws ec2 modify-security-group-rules --group-id "$SG" --security-group-rules "$(jq -cn \
    --arg id "$id" --arg c "$3" --argjson p "$2" --arg d "$1" \
    '[{SecurityGroupRuleId: $id, SecurityGroupRule: {IpProtocol: "tcp", FromPort: $p, ToPort: $p, CidrIpv4: $c, Description: $d}}]')" >/dev/null
  report changed "$id ($1) now allows tcp/$2 from $3, was $cidr"
}
