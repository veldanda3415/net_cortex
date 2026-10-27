module "vpc" {
  source = "../vpc"

  name       = "security"
  cidr_block = var.cidr_block
  subnets = {
    "public-a" = { cidr = cidrsubnet(var.cidr_block, 8, 0), az = var.az_names[0], role = "public" }
    "fw-a"     = { cidr = cidrsubnet(var.cidr_block, 8, 10), az = var.az_names[0], role = "fw" }
    "fw-b"     = { cidr = cidrsubnet(var.cidr_block, 8, 11), az = var.az_names[1], role = "fw" }
    "tgw-a"    = { cidr = cidrsubnet(var.cidr_block, 12, 4080), az = var.az_names[0], role = "tgw" }
    "tgw-b"    = { cidr = cidrsubnet(var.cidr_block, 12, 4081), az = var.az_names[1], role = "tgw" }
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = module.vpc.vpc_id

  tags = { Name = "netcortex-lab-security-igw" }
}

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = { Name = "netcortex-lab-nat-a" }

  depends_on = [aws_internet_gateway.this]
}

# Single NAT in AZ a (spec §3): tgw-b reaches it cross-AZ, which is acceptable for a lab.
resource "aws_nat_gateway" "a" {
  allocation_id = aws_eip.nat.id
  subnet_id     = module.vpc.subnet_ids["public-a"]

  tags = { Name = "netcortex-lab-nat-a" }
}

# Appliance mode keeps both directions of a flow in the same AZ once firewalls
# exist (spec D4); turning it off is one of the catalogued faults.
resource "aws_ec2_transit_gateway_vpc_attachment" "this" {
  transit_gateway_id                              = var.transit_gateway_id
  vpc_id                                          = module.vpc.vpc_id
  subnet_ids                                      = module.vpc.subnet_ids_by_role["tgw"]
  appliance_mode_support                          = "enable"
  dns_support                                     = "enable"
  transit_gateway_default_route_table_association = false
  transit_gateway_default_route_table_propagation = false

  tags = { Name = "netcortex-lab-attach-security" }
}

# --- public subnet: out to the internet, back to the TGW for lab ranges ---

resource "aws_route" "public_default" {
  route_table_id         = module.vpc.route_table_ids["public-a"]
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route" "public_return" {
  for_each = toset(var.return_cidrs)

  route_table_id         = module.vpc.route_table_ids["public-a"]
  destination_cidr_block = each.value
  transit_gateway_id     = var.transit_gateway_id

  depends_on = [aws_ec2_transit_gateway_vpc_attachment.this]
}

# --- TGW subnets: inspection off sends everything straight to the NAT ---
# Inspection on (0/0 -> same-AZ firewall ENI, fw subnets -> NAT) lands with the
# firewalls in Stage 1 PR 5; until then the root refuses enable_inspection = true.

resource "aws_route" "tgw_default" {
  for_each = module.vpc.route_table_ids_by_role["tgw"]

  route_table_id         = each.value
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.a.id
}
