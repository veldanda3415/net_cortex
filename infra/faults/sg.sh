# Security group rule helpers for the app web servers. Source after lib.sh.
SG=$(t .app_web_sg_id)

# rule_id <cidr> <port>: the inbound TCP rule for that source and port, if any.
rule_id() {
  aws ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG" \
    --query "SecurityGroupRules[?!IsEgress && CidrIpv4=='$1' && IpProtocol=='tcp' && FromPort==\`$2\`] | [0].SecurityGroupRuleId" \
    --output text
}

# revoke_rule <cidr> <port> / restore_rule <cidr> <port> <description>
revoke_rule() {
  local id
  id=$(rule_id "$1" "$2")
  if [ "$id" = None ]; then report already "no tcp/$2 from $1 on $SG"; exit 0; fi
  aws ec2 revoke-security-group-ingress --group-id "$SG" --security-group-rule-ids "$id" >/dev/null
  report changed "revoked $id (tcp/$2 from $1) on $SG"
}
restore_rule() {
  if [ "$(rule_id "$1" "$2")" != None ]; then report already "tcp/$2 from $1 already on $SG"; exit 0; fi
  aws ec2 authorize-security-group-ingress --group-id "$SG" \
    --ip-permissions "$(jq -cn --arg c "$1" --argjson p "$2" --arg d "$3" \
      '[{IpProtocol: "tcp", FromPort: $p, ToPort: $p, IpRanges: [{CidrIp: $c, Description: $d}]}]')" >/dev/null
  report changed "restored tcp/$2 from $1 on $SG"
}
