output "instance_id" {
  description = "Instance ID (SSM target)."
  value       = aws_instance.this.id
}

output "private_ip" {
  description = "Primary private IP."
  value       = aws_instance.this.private_ip
}
