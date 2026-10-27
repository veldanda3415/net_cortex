# "On-prem" data centre (spec §3). Deliberately NOT attached to the TGW (spec D1):
# it reaches AWS only through the Site-to-Site VPN, so VPN/BGP faults are visible.

locals {
  onprem_cidr = "172.16.0.0/16"
  lan_cidr    = "172.16.10.0/24"
}

module "onprem_vpc" {
  source = "../../modules/vpc"

  name       = "onprem"
  cidr_block = local.onprem_cidr
  subnets = {
    "wan" = { cidr = "172.16.0.0/24", az = local.az_names[0], role = "public" }
    "lan" = { cidr = local.lan_cidr, az = local.az_names[0], role = "private" }
  }
}

resource "aws_internet_gateway" "onprem" {
  vpc_id = module.onprem_vpc.vpc_id

  tags = { Name = "netcortex-lab-onprem-igw" }
}

resource "aws_route" "onprem_wan_default" {
  route_table_id         = module.onprem_vpc.route_table_ids["wan"]
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.onprem.id
}

# The LAN sends everything to the router: 10/8 goes into the tunnels, the rest
# is MASQUERADEd out of the WAN interface.
resource "aws_route" "onprem_lan_default" {
  route_table_id         = module.onprem_vpc.route_table_ids["lan"]
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = module.onprem_router.lan_network_interface_id
}

# The EIP exists before the router so the customer gateway can point at it.
resource "aws_eip" "router" {
  domain = "vpc"

  tags = { Name = "netcortex-lab-onprem-router" }

  depends_on = [aws_internet_gateway.onprem]
}

module "onprem_router" {
  source = "../../modules/onprem_router"

  region            = var.region
  vpc_id            = module.onprem_vpc.vpc_id
  wan_subnet_id     = module.onprem_vpc.subnet_ids["wan"]
  lan_subnet_id     = module.onprem_vpc.subnet_ids["lan"]
  lan_cidr          = local.lan_cidr
  onprem_cidr       = local.onprem_cidr
  eip_allocation_id = aws_eip.router.id
  eip_public_ip     = aws_eip.router.public_ip
  ami_id            = data.aws_ssm_parameter.ubuntu_arm64.insecure_value
  instance_type     = var.router_instance_type
  onprem_asn        = var.onprem_asn
  aws_asn           = 64512

  # bgp_active_standby (spec §3 default): tunnel 1 preferred both ways.
  # Tunnel 2 is prepended outbound and gets lower local-pref inbound.
  tunnels = [
    for i in range(2) : {
      outside_ip      = module.vpn.tunnel_outside_ips[i]
      inside_cidr     = var.tunnel_inside_cidrs[i]
      psk_parameter   = module.vpn.psk_parameter_names[i]
      as_path_prepend = i == 0 ? 0 : 3
      local_pref      = i == 0 ? 200 : 100
    }
  ]
  psk_parameter_arns = module.vpn.psk_parameter_arns

  depends_on = [aws_route.onprem_wan_default]
}
