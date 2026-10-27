output "vpc_id" {
  description = "Security VPC ID."
  value       = module.vpc.vpc_id
}

output "attachment_id" {
  description = "TGW attachment ID for the security VPC."
  value       = aws_ec2_transit_gateway_vpc_attachment.this.id
}

output "nat_gateway_id" {
  description = "NAT gateway ID (AZ a)."
  value       = aws_nat_gateway.a.id
}

output "nat_public_ip" {
  description = "NAT gateway public IP."
  value       = aws_eip.nat.public_ip
}

output "subnet_ids" {
  description = "Subnet IDs keyed by subnet name."
  value       = module.vpc.subnet_ids
}

output "route_table_ids" {
  description = "Route table IDs keyed by subnet name."
  value       = module.vpc.route_table_ids
}
