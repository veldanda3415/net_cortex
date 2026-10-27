module "dns" {
  source = "../../modules/dns"

  zone_name = var.dns_zone_name
  vpc_ids = {
    app      = module.app_vpc.vpc_id
    shared   = module.shared_vpc.vpc_id
    security = module.security_vpc.vpc_id
  }

  alias_records = {
    app = { dns_name = aws_lb.app.dns_name, zone_id = aws_lb.app.zone_id }
  }
  a_records = merge(
    {
      "app-a" = [module.app["a"].private_ip]
      "app-b" = [module.app["b"].private_ip]
      "svc"   = [module.svc.private_ip]
    },
    var.enable_peering ? { partner = [aws_instance.partner[0].private_ip] } : {},
  )

  query_log_vpc_ids = {
    app    = module.app_vpc.vpc_id
    shared = module.shared_vpc.vpc_id
  }
  retention_days = var.dns_query_log_retention_days
}
