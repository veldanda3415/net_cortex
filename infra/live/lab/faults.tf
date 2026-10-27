# Fault injection support (spec §7 PR 6). The scripts in infra/faults/ write one
# event per inject/revert here. This log group is the ground truth for evals:
# NetCortex must never read it, or the RCA would just be reading the answer key.

resource "aws_cloudwatch_log_group" "faults" {
  name              = "/netcortex-lab/faults"
  retention_in_days = 30
}

output "fault_targets" {
  description = "Everything the fault scripts in infra/faults/ act on, in one place."
  value = {
    region            = var.region
    vpn_mode          = var.vpn_mode
    enable_inspection = var.enable_inspection
    enable_peering    = var.enable_peering
    log_group         = aws_cloudwatch_log_group.faults.name

    router_instance_id = module.onprem_router.instance_id
    app_a_instance_id  = module.app["a"].instance_id
    bgp_neighbors      = local.bgp_neighbors
    aws_asn            = 64512
    onprem_asn         = var.onprem_asn
    onprem_cidr        = local.onprem_cidr
    app_cidr           = local.app_cidr

    vpn_connection_id      = module.vpn.vpn_connection_id
    vpn_tunnel_outside_ips = module.vpn.tunnel_outside_ips

    tgw_route_table_ids    = module.tgw.route_table_ids
    app_attachment_id      = aws_ec2_transit_gateway_vpc_attachment.spoke["app"].id
    security_attachment_id = module.security_vpc.attachment_id
    app_private_subnet_a   = module.app_vpc.subnet_ids["private-a"]

    app_vpc_id    = module.app_vpc.vpc_id
    app_web_sg_id = aws_security_group.app_web.id

    nat_gateway_id               = module.security_vpc.nat_gateway_id
    security_tgw_route_table_ids = { for k, id in module.security_vpc.route_table_ids : k => id if startswith(k, "tgw-") }
    app_private_route_table_ids  = module.app_vpc.route_table_ids_by_role["private"]
    peering_connection_id        = one(aws_vpc_peering_connection.app_partner[*].id)
    partner_cidr                 = var.enable_peering ? local.partner_vpc.cidr_block : null
    firewall_instance_ids        = var.enable_inspection ? module.firewall[0].instance_ids : {}

    dns_zone_id  = module.dns.zone_id
    dns_zone     = var.dns_zone_name
    nlb_dns_name = aws_lb.app.dns_name
    nlb_zone_id  = aws_lb.app.zone_id
  }
}
