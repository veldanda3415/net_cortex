module "tgw" {
  source = "../../modules/tgw"
}

module "app_vpc" {
  source = "../../modules/vpc"

  name       = "app"
  cidr_block = "10.10.0.0/16"
  subnets = {
    "private-a" = { cidr = "10.10.0.0/20", az = local.az_names[0], role = "private" }
    "private-b" = { cidr = "10.10.16.0/20", az = local.az_names[1], role = "private" }
    "tgw-a"     = { cidr = "10.10.255.0/28", az = local.az_names[0], role = "tgw" }
    "tgw-b"     = { cidr = "10.10.255.16/28", az = local.az_names[1], role = "tgw" }
  }
}

module "shared_vpc" {
  source = "../../modules/vpc"

  name       = "shared"
  cidr_block = "10.20.0.0/16"
  subnets = {
    # /19s leave room for EKS VPC-CNI pod IPs in Stage 2.
    "private-a" = { cidr = "10.20.0.0/19", az = local.az_names[0], role = "private" }
    "private-b" = { cidr = "10.20.32.0/19", az = local.az_names[1], role = "private" }
    "tgw-a"     = { cidr = "10.20.255.0/28", az = local.az_names[0], role = "tgw" }
    "tgw-b"     = { cidr = "10.20.255.16/28", az = local.az_names[1], role = "tgw" }
  }
}

module "security_vpc" {
  source = "../../modules/security_vpc"

  az_names           = local.az_names
  transit_gateway_id = module.tgw.id
  firewall_eni_ids   = var.enable_inspection ? module.firewall[0].network_interface_ids : {}
}

locals {
  spoke_vpcs = {
    app    = module.app_vpc
    shared = module.shared_vpc
  }

  # Private route tables of both spokes, keyed "<vpc>/<subnet>".
  spoke_private_route_tables = merge([
    for vpc, m in local.spoke_vpcs : {
      for subnet, rt in m.route_table_ids_by_role["private"] : "${vpc}/${subnet}" => rt
    }
  ]...)
}

resource "aws_ec2_transit_gateway_vpc_attachment" "spoke" {
  for_each = local.spoke_vpcs

  transit_gateway_id                              = module.tgw.id
  vpc_id                                          = each.value.vpc_id
  subnet_ids                                      = each.value.subnet_ids_by_role["tgw"]
  dns_support                                     = "enable"
  transit_gateway_default_route_table_association = false
  transit_gateway_default_route_table_propagation = false

  tags = { Name = "netcortex-lab-attach-${each.key}" }
}

# Spokes send everything to the TGW; the TGW decides between east-west and egress.
resource "aws_route" "spoke_default" {
  for_each = local.spoke_private_route_tables

  route_table_id         = each.value
  destination_cidr_block = "0.0.0.0/0"
  transit_gateway_id     = module.tgw.id

  depends_on = [aws_ec2_transit_gateway_vpc_attachment.spoke]
}
