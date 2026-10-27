module "flowlogs" {
  source = "../../modules/flowlogs"

  vpcs = merge(
    {
      app      = module.app_vpc.vpc_id
      shared   = module.shared_vpc.vpc_id
      security = module.security_vpc.vpc_id
      onprem   = module.onprem_vpc.vpc_id
    },
    var.enable_peering ? { partner = module.partner_vpc[0].vpc_id } : {},
  )
  transit_gateways = { core = module.tgw.id }
  retention_days   = var.flow_log_retention_days
}
