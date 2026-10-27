# Private hosted zone helpers. Source after lib.sh.
ZONE=$(t .dns_zone_id)
NAME="app.$(t .dns_zone)."

record_exists() {
  [ "$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE" \
    --query "length(ResourceRecordSets[?Name=='$NAME' && Type=='A'])")" -gt 0 ]
}

# change_alias <DELETE|UPSERT>: the alias record must match exactly to be deleted.
change_alias() {
  aws route53 change-resource-record-sets --hosted-zone-id "$ZONE" --change-batch "$(jq -cn \
    --arg a "$1" --arg n "$NAME" --arg d "$(t .nlb_dns_name)" --arg z "$(t .nlb_zone_id)" \
    '{Changes: [{Action: $a, ResourceRecordSet: {Name: $n, Type: "A",
      AliasTarget: {HostedZoneId: $z, DNSName: $d, EvaluateTargetHealth: true}}}]}')" >/dev/null
}
