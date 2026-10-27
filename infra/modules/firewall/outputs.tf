output "network_interface_ids" {
  description = "Firewall ENI per AZ letter; the TGW subnets route 0/0 here."
  value       = { for k, eni in aws_network_interface.this : k => eni.id }
}

output "instance_ids" {
  description = "Firewall instance IDs per AZ letter (SSM targets)."
  value       = { for k, i in aws_instance.this : k => i.id }
}

output "log_group_name" {
  description = "Firewall drop log group."
  value       = aws_cloudwatch_log_group.drops.name
}

output "metric_namespace" {
  description = "Firewall metric namespace."
  value       = var.metric_namespace
}
