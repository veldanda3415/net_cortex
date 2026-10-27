output "zone_id" {
  description = "Private hosted zone ID."
  value       = aws_route53_zone.this.zone_id
}

output "query_log_group_name" {
  description = "Resolver query log group."
  value       = aws_cloudwatch_log_group.queries.name
}
