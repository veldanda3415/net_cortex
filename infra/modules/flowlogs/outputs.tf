output "log_group_names" {
  description = "Flow log CloudWatch log group names keyed by VPC name."
  value       = { for k, lg in aws_cloudwatch_log_group.this : k => lg.name }
}

output "tgw_log_group_names" {
  description = "TGW flow log CloudWatch log group names keyed by TGW name."
  value       = { for k, lg in aws_cloudwatch_log_group.tgw : k => lg.name }
}
