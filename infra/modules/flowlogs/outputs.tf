output "log_group_names" {
  description = "Flow log CloudWatch log group names keyed by VPC name."
  value       = { for k, lg in aws_cloudwatch_log_group.this : k => lg.name }
}
