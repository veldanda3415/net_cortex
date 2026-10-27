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

output "router_instance_id" {
  description = "On-prem router instance ID (SSM target)."
  value       = module.onprem_router.instance_id
}

output "router_public_ip" {
  description = "On-prem router Elastic IP (the customer gateway address)."
  value       = aws_eip.router.public_ip
}

output "vpn_connection_id" {
  description = "Site-to-Site VPN connection ID."
  value       = module.vpn.vpn_connection_id
}

output "vpn_tunnel_outside_ips" {
  description = "AWS tunnel endpoint IPs, tunnel 1/2 order."
  value       = module.vpn.tunnel_outside_ips
}

output "vpn_log_groups" {
  description = "VPN tunnel activity and BGP log groups."
  value       = module.vpn.log_group_names
}

output "nlb_dns_name" {
  description = "Internal NLB DNS name (on-prem uses this; the VPCs use app.<dns_zone_name>)."
  value       = aws_lb.app.dns_name
}

output "workload_instance_ids" {
  description = "Workload instance IDs (SSM targets) keyed by name."
  value = {
    app-a  = module.app["a"].instance_id
    app-b  = module.app["b"].instance_id
    svc    = module.svc.instance_id
    client = module.client.instance_id
  }
}

output "dns_query_log_group" {
  description = "Route 53 Resolver query log group (app and shared VPCs)."
  value       = module.dns.query_log_group_name
}

output "dashboard_url" {
  description = "CloudWatch dashboard with every flow, the NLB targets and the VPN tunnels."
  value       = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.lab.dashboard_name}"
}

output "tgw_flow_log_groups" {
  description = "TGW Flow Log groups keyed by TGW name."
  value       = module.flowlogs.tgw_log_group_names
}

output "router_log_groups" {
  description = "Router FRR and charon log groups."
  value       = module.onprem_router.log_group_names
}

output "network_manager_events_log_group" {
  description = "Network Manager events, forwarded from us-west-2."
  value       = aws_cloudwatch_log_group.network_manager_events.name
}

output "global_network_id" {
  description = "Network Manager global network ID (us-west-2)."
  value       = aws_networkmanager_global_network.lab.id
}
