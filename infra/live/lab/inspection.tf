# Inspection firewalls (spec D3/D4), built only with enable_inspection = true.
# The same flag swaps the TGW route tables (tgw_routing.tf) and points the
# security VPC's TGW subnets at these firewalls instead of the NAT.

module "firewall" {
  source = "../../modules/firewall"
  count  = var.enable_inspection ? 1 : 0

  region     = var.region
  vpc_id     = module.security_vpc.vpc_id
  subnet_ids = module.security_vpc.fw_subnet_ids
  ami_id     = data.aws_ssm_parameter.ubuntu_arm64.insecure_value
  cidrs = {
    cloud  = "10.0.0.0/8"
    app    = module.app_vpc.cidr_block
    shared = module.shared_vpc.cidr_block
    onprem = local.onprem_cidr
  }
  log_retention_days = var.flow_log_retention_days
}
