# Private hosted zone. On-prem does not resolve it: a Resolver inbound endpoint
# costs ~$0.25/hr, so on-prem clients use the NLB's own DNS name instead.
resource "aws_route53_zone" "this" {
  name    = var.zone_name
  comment = "netcortex-lab private zone"

  # Fault scripts may add records; never block an ephemeral destroy on them.
  force_destroy = true

  dynamic "vpc" {
    for_each = var.vpc_ids

    content {
      vpc_id = vpc.value
    }
  }

  tags = { Name = "netcortex-lab-${var.zone_name}" }
}

resource "aws_route53_record" "alias" {
  for_each = var.alias_records

  zone_id = aws_route53_zone.this.zone_id
  name    = each.key
  type    = "A"

  alias {
    name                   = each.value.dns_name
    zone_id                = each.value.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "a" {
  for_each = var.a_records

  zone_id = aws_route53_zone.this.zone_id
  name    = each.key
  type    = "A"
  ttl     = 60
  records = each.value
}

# --- Resolver query logging (NXDOMAIN/SERVFAIL evidence for the RCA) ---
# Delivery uses the AWSServiceRoleForRoute53Resolver service-linked role.

resource "aws_cloudwatch_log_group" "queries" {
  name              = "/netcortex-lab/dns/resolver-queries"
  retention_in_days = var.retention_days
}

resource "aws_route53_resolver_query_log_config" "this" {
  name            = "netcortex-lab"
  destination_arn = aws_cloudwatch_log_group.queries.arn

  tags = { Name = "netcortex-lab-resolver-queries" }
}

resource "aws_route53_resolver_query_log_config_association" "this" {
  for_each = var.query_log_vpc_ids

  resolver_query_log_config_id = aws_route53_resolver_query_log_config.this.id
  resource_id                  = each.value
}
