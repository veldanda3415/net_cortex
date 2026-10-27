# Partner VPC (spec §3): peered with app only. Peering is not transitive, so
# on-prem and shared must NOT reach it; the client checks that continuously.
# The partner has no internet path at all (no IGW, no NAT, no TGW), so its web
# server uses only what is on the base image, and it is not reachable over SSM.

module "partner_vpc" {
  source = "../../modules/vpc"
  count  = var.enable_peering ? 1 : 0

  name       = "partner"
  cidr_block = "10.40.0.0/16"
  subnets = {
    "private-a" = { cidr = "10.40.0.0/24", az = local.az_names[0], role = "private" }
  }
}

locals {
  partner_vpc = var.enable_peering ? module.partner_vpc[0] : null
}

resource "aws_vpc_peering_connection" "app_partner" {
  count = var.enable_peering ? 1 : 0

  vpc_id      = module.app_vpc.vpc_id
  peer_vpc_id = local.partner_vpc.vpc_id
  auto_accept = true

  tags = { Name = "netcortex-lab-app-partner" }
}

resource "aws_route" "app_to_partner" {
  for_each = var.enable_peering ? module.app_vpc.route_table_ids_by_role["private"] : {}

  route_table_id            = each.value
  destination_cidr_block    = local.partner_vpc.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.app_partner[0].id
}

resource "aws_route" "partner_to_app" {
  count = var.enable_peering ? 1 : 0

  route_table_id            = local.partner_vpc.route_table_ids["private-a"]
  destination_cidr_block    = module.app_vpc.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.app_partner[0].id
}

resource "aws_security_group" "partner" {
  count = var.enable_peering ? 1 : 0

  name        = "netcortex-lab-partner"
  description = "Partner web server: HTTP from the app VPC only"
  vpc_id      = local.partner_vpc.vpc_id

  tags = { Name = "netcortex-lab-partner" }
}

resource "aws_vpc_security_group_ingress_rule" "partner_http_app" {
  count = var.enable_peering ? 1 : 0

  security_group_id = aws_security_group.partner[0].id
  description       = "HTTP from the app VPC (over peering)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = module.app_vpc.cidr_block
}

resource "aws_vpc_security_group_egress_rule" "partner_app" {
  count = var.enable_peering ? 1 : 0

  security_group_id = aws_security_group.partner[0].id
  description       = "Replies to the app VPC"
  ip_protocol       = "-1"
  cidr_ipv4         = module.app_vpc.cidr_block
}

resource "aws_instance" "partner" {
  count = var.enable_peering ? 1 : 0

  ami                         = data.aws_ssm_parameter.ubuntu_arm64.insecure_value
  instance_type               = "t4g.nano"
  subnet_id                   = local.partner_vpc.subnet_ids["private-a"]
  vpc_security_group_ids      = [aws_security_group.partner[0].id]
  user_data_replace_on_change = true

  user_data = "#cloud-config\n${yamlencode({
    hostname = "partner"
    write_files = [
      { path = "/var/www/partner/index.html", permissions = "0644", content = "partner\n" },
      {
        path        = "/etc/systemd/system/netcortex-partner-web.service"
        permissions = "0644"
        content     = <<-UNIT
          [Unit]
          Description=NetCortex partner web server (no packages: python3 http.server)
          After=network-online.target

          [Service]
          ExecStart=/usr/bin/python3 -m http.server 80 --directory /var/www/partner
          Restart=always

          [Install]
          WantedBy=multi-user.target
        UNIT
      },
    ]
    runcmd = [
      ["systemctl", "daemon-reload"],
      ["systemctl", "enable", "--now", "netcortex-partner-web.service"],
    ]
  })}"

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = { Name = "netcortex-lab-partner" }
}
