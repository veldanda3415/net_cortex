resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "netcortex-lab-${var.name}" }
}

# Lock down the default SG so nothing silently relies on it.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "netcortex-lab-${var.name}-default-unused" }
}

resource "aws_subnet" "this" {
  for_each = var.subnets

  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az

  tags = {
    Name = "netcortex-lab-${var.name}-${each.key}"
    Role = each.value.role
  }
}

# One route table per subnet: routing faults can then target a single subnet,
# and every route is explicit rather than inherited from the main table.
resource "aws_route_table" "this" {
  for_each = var.subnets

  vpc_id = aws_vpc.this.id

  tags = { Name = "netcortex-lab-${var.name}-${each.key}" }
}

resource "aws_route_table_association" "this" {
  for_each = var.subnets

  subnet_id      = aws_subnet.this[each.key].id
  route_table_id = aws_route_table.this[each.key].id
}
