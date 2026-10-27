output "az_names" {
  description = "AZ names resolved from the pinned AZ IDs, in a/b order."
  value       = local.az_names
}

output "transit_gateway_id" {
  description = "Core TGW ID."
  value       = module.tgw.id
}

output "tgw_route_table_ids" {
  description = "TGW route table IDs (spoke, onprem, security)."
  value       = module.tgw.route_table_ids
}

output "vpc_ids" {
  description = "VPC IDs keyed by VPC name."
  value = {
    app      = module.app_vpc.vpc_id
    shared   = module.shared_vpc.vpc_id
    security = module.security_vpc.vpc_id
  }
}

output "nat_public_ip" {
  description = "Public IP that all lab egress leaves from."
  value       = module.security_vpc.nat_public_ip
}

output "flow_log_groups" {
  description = "VPC Flow Log groups keyed by VPC name."
  value       = module.flowlogs.log_group_names
}

output "test_instance_id" {
  description = "Temporary test instance ID (null when enable_test_instance = false)."
  value       = one(aws_instance.test[*].id)
}
