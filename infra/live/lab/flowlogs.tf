module "flowlogs" {
  source = "../../modules/flowlogs"

  vpcs = {
    app      = module.app_vpc.vpc_id
    shared   = module.shared_vpc.vpc_id
    security = module.security_vpc.vpc_id
    onprem   = module.onprem_vpc.vpc_id
  }
  retention_days = var.flow_log_retention_days
}
