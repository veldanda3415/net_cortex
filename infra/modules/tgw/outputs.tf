output "id" {
  description = "Transit gateway ID."
  value       = aws_ec2_transit_gateway.this.id
}

output "route_table_ids" {
  description = "TGW route table IDs keyed by spoke, onprem, security."
  value       = { for k, rt in aws_ec2_transit_gateway_route_table.this : k => rt.id }
}
