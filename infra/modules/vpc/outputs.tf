output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.this.id
}

output "cidr_block" {
  description = "VPC IPv4 CIDR."
  value       = aws_vpc.this.cidr_block
}

output "subnet_ids" {
  description = "Subnet IDs keyed by subnet name."
  value       = { for k, s in aws_subnet.this : k => s.id }
}

output "route_table_ids" {
  description = "Route table IDs keyed by subnet name."
  value       = { for k, rt in aws_route_table.this : k => rt.id }
}

output "subnet_ids_by_role" {
  description = "Subnet IDs grouped by role, ordered by subnet name."
  value = {
    for role in distinct([for s in var.subnets : s.role]) :
    role => [for k in sort(keys(var.subnets)) : aws_subnet.this[k].id if var.subnets[k].role == role]
  }
}

output "route_table_ids_by_role" {
  description = "Route table IDs grouped by role, keyed by subnet name."
  value = {
    for role in distinct([for s in var.subnets : s.role]) :
    role => { for k, s in var.subnets : k => aws_route_table.this[k].id if s.role == role }
  }
}
