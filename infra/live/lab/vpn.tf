module "vpn" {
  source = "../../modules/vpn"

  transit_gateway_id  = module.tgw.id
  customer_gateway_ip = aws_eip.router.public_ip
  onprem_asn          = var.onprem_asn
  tunnel_inside_cidrs = var.tunnel_inside_cidrs
  static_routes_only  = var.vpn_mode == "static"
}
