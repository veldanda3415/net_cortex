# Default association/propagation are off: every attachment is placed in a route
# table explicitly by the caller, so routing is readable from code alone.
resource "aws_ec2_transit_gateway" "this" {
  description                     = "netcortex-lab core TGW"
  amazon_side_asn                 = var.amazon_side_asn
  default_route_table_association = "disable"
  default_route_table_propagation = "disable"
  vpn_ecmp_support                = "enable"
  dns_support                     = "enable"
  auto_accept_shared_attachments  = "disable"

  tags = { Name = "netcortex-lab-tgw" }
}

resource "aws_ec2_transit_gateway_route_table" "this" {
  for_each = toset(["spoke", "onprem", "security"])

  transit_gateway_id = aws_ec2_transit_gateway.this.id

  tags = { Name = "netcortex-lab-rt-${each.key}" }
}
