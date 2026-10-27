# TGW route tables (spec §3). One boolean, enable_inspection, swaps propagated
# routes for static routes to the security VPC: that is how inspection insertion
# works in production.

locals {
  rt = module.tgw.route_table_ids

  security_attachment_id = module.security_vpc.attachment_id

  spoke_attachment_ids = { for k, a in aws_ec2_transit_gateway_vpc_attachment.spoke : k => a.id }

  vpn_attachment_id = module.vpn.transit_gateway_attachment_id
}

# --- associations ---

resource "aws_ec2_transit_gateway_route_table_association" "spoke" {
  for_each = local.spoke_attachment_ids

  transit_gateway_attachment_id  = each.value
  transit_gateway_route_table_id = local.rt["spoke"]
}

resource "aws_ec2_transit_gateway_route_table_association" "vpn" {
  transit_gateway_attachment_id  = local.vpn_attachment_id
  transit_gateway_route_table_id = local.rt["onprem"]
}

resource "aws_ec2_transit_gateway_route_table_association" "security" {
  transit_gateway_attachment_id  = local.security_attachment_id
  transit_gateway_route_table_id = local.rt["security"]
}

# --- rt-spoke: 0/0 to security always; direct east-west only with inspection off ---

resource "aws_ec2_transit_gateway_route" "spoke_default" {
  destination_cidr_block         = "0.0.0.0/0"
  transit_gateway_attachment_id  = local.security_attachment_id
  transit_gateway_route_table_id = local.rt["spoke"]
}

resource "aws_ec2_transit_gateway_route_table_propagation" "spoke_from_spokes" {
  for_each = var.enable_inspection ? {} : local.spoke_attachment_ids

  transit_gateway_attachment_id  = each.value
  transit_gateway_route_table_id = local.rt["spoke"]
}

resource "aws_ec2_transit_gateway_route_table_propagation" "spoke_from_vpn" {
  count = var.enable_inspection ? 0 : 1

  transit_gateway_attachment_id  = local.vpn_attachment_id
  transit_gateway_route_table_id = local.rt["spoke"]
}

# --- rt-onprem: spokes propagated with inspection off, 10/8 via security with it on ---

resource "aws_ec2_transit_gateway_route_table_propagation" "onprem_from_spokes" {
  for_each = var.enable_inspection ? {} : local.spoke_attachment_ids

  transit_gateway_attachment_id  = each.value
  transit_gateway_route_table_id = local.rt["onprem"]
}

resource "aws_ec2_transit_gateway_route" "onprem_via_security" {
  count = var.enable_inspection ? 1 : 0

  destination_cidr_block         = "10.0.0.0/8"
  transit_gateway_attachment_id  = local.security_attachment_id
  transit_gateway_route_table_id = local.rt["onprem"]
}

# --- rt-security: return paths to every spoke and to on-prem ---

resource "aws_ec2_transit_gateway_route_table_propagation" "security_from_spokes" {
  for_each = local.spoke_attachment_ids

  transit_gateway_attachment_id  = each.value
  transit_gateway_route_table_id = local.rt["security"]
}

resource "aws_ec2_transit_gateway_route_table_propagation" "security_from_vpn" {
  transit_gateway_attachment_id  = local.vpn_attachment_id
  transit_gateway_route_table_id = local.rt["security"]
}
