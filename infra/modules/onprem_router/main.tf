locals {
  # IKE/ESP arrive from the two AWS tunnel endpoints only.
  ike_ports = { ike = 500, natt = 4500 }
  ike_rules = {
    for pair in setproduct(range(length(var.tunnels)), keys(local.ike_ports)) :
    "t${pair[0] + 1}-${pair[1]}" => {
      cidr = "${var.tunnels[pair[0]].outside_ip}/32"
      port = local.ike_ports[pair[1]]
    }
  }

  router_config = {
    region         = var.region
    wan_private_ip = aws_network_interface.wan.private_ip
    public_ip      = var.eip_public_ip
    lan_mac        = aws_network_interface.lan.mac_address
    lan_ip         = aws_network_interface.lan.private_ip
    lan_cidr       = var.lan_cidr
    onprem_cidr    = var.onprem_cidr
    onprem_asn     = var.onprem_asn
    aws_asn        = var.aws_asn
    mtu            = var.mtu
    mss            = var.mss

    metric_namespace = var.metric_namespace
    log_groups       = { for k, lg in aws_cloudwatch_log_group.router : k => lg.name }
    tunnels = [
      for i, t in var.tunnels : {
        outside_ip      = t.outside_ip
        inside_local    = cidrhost(t.inside_cidr, 2)
        inside_remote   = cidrhost(t.inside_cidr, 1)
        psk_parameter   = t.psk_parameter
        mark            = 100 + i
        as_path_prepend = t.as_path_prepend
        local_pref      = t.local_pref
      }
    ]
  }

  user_data = "#cloud-config\n${yamlencode({
    package_update = true
    packages       = ["strongswan-swanctl", "charon-systemd", "frr", "jq", "iptables"]
    snap           = { commands = ["snap install aws-cli --classic"] }
    write_files = [
      { path = "/etc/netcortex/router.json", permissions = "0644", content = jsonencode(local.router_config) },
      { path = "/usr/local/sbin/netcortex-router-up", permissions = "0755", content = file("${path.module}/router-up.sh") },
      { path = "/etc/systemd/system/netcortex-router.service", permissions = "0644", content = file("${path.module}/netcortex-router.service") },
      { path = "/usr/local/sbin/netcortex-router-telemetry", permissions = "0755", content = file("${path.module}/router-telemetry.sh") },
      { path = "/usr/local/sbin/netcortex-bgp-metrics", permissions = "0755", content = file("${path.module}/bgp-metrics.sh") },
      { path = "/etc/systemd/system/netcortex-router-telemetry.service", permissions = "0644", content = file("${path.module}/netcortex-router-telemetry.service") },
      { path = "/etc/systemd/system/netcortex-bgp-metrics.service", permissions = "0644", content = file("${path.module}/netcortex-bgp-metrics.service") },
      { path = "/etc/systemd/system/netcortex-bgp-metrics.timer", permissions = "0644", content = file("${path.module}/netcortex-bgp-metrics.timer") },
    ]
    runcmd = [
      ["systemctl", "daemon-reload"],
      ["systemctl", "enable", "--now", "--no-block", "netcortex-router-telemetry.service"],
      ["systemctl", "enable", "--now", "netcortex-router.service"],
    ]
  })}"
}

# --- security groups ---

resource "aws_security_group" "wan" {
  name        = "netcortex-lab-onprem-router-wan"
  description = "Router WAN: IKE and NAT-T from the AWS tunnel endpoints only"
  vpc_id      = var.vpc_id

  tags = { Name = "netcortex-lab-onprem-router-wan" }
}

resource "aws_vpc_security_group_ingress_rule" "wan_ike" {
  for_each = local.ike_rules

  security_group_id = aws_security_group.wan.id
  description       = "IKE/NAT-T from AWS tunnel ${each.key}"
  ip_protocol       = "udp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}

resource "aws_vpc_security_group_egress_rule" "wan_all" {
  security_group_id = aws_security_group.wan.id
  description       = "All outbound (IKE, packages, SSM)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "lan" {
  name        = "netcortex-lab-onprem-router-lan"
  description = "Router LAN: everything from on-prem hosts (they route via the router)"
  vpc_id      = var.vpc_id

  tags = { Name = "netcortex-lab-onprem-router-lan" }
}

resource "aws_vpc_security_group_ingress_rule" "lan_all" {
  security_group_id = aws_security_group.lan.id
  description       = "All from the on-prem LAN"
  ip_protocol       = "-1"
  cidr_ipv4         = var.lan_cidr
}

resource "aws_vpc_security_group_egress_rule" "lan_all" {
  security_group_id = aws_security_group.lan.id
  description       = "All outbound to the LAN"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# --- interfaces: a router forwards, so source/dest check is off on both ---

resource "aws_network_interface" "wan" {
  subnet_id         = var.wan_subnet_id
  security_groups   = [aws_security_group.wan.id]
  source_dest_check = false

  tags = { Name = "netcortex-lab-onprem-router-wan" }
}

resource "aws_network_interface" "lan" {
  subnet_id         = var.lan_subnet_id
  security_groups   = [aws_security_group.lan.id]
  source_dest_check = false

  tags = { Name = "netcortex-lab-onprem-router-lan" }
}

# Associated before boot so cloud-init can reach package mirrors and SSM.
resource "aws_eip_association" "wan" {
  allocation_id        = var.eip_allocation_id
  network_interface_id = aws_network_interface.wan.id
}

# --- IAM: SSM access plus read of the two PSK parameters, nothing else ---

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "router" {
  name               = "netcortex-lab-onprem-router"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.router.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "psk" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = var.psk_parameter_arns
  }
}

resource "aws_iam_role_policy" "psk" {
  name   = "read-vpn-psks"
  role   = aws_iam_role.router.id
  policy = data.aws_iam_policy_document.psk.json
}

# --- telemetry: FRR/charon logs via the CloudWatch agent, BGP metrics via the timer ---

resource "aws_cloudwatch_log_group" "router" {
  for_each = toset(["frr", "charon"])

  name              = "/netcortex-lab/router/${each.key}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "telemetry" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = flatten([for lg in aws_cloudwatch_log_group.router : [lg.arn, "${lg.arn}:*"]])
  }

  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.metric_namespace]
    }
  }
}

resource "aws_iam_role_policy" "telemetry" {
  name   = "router-telemetry"
  role   = aws_iam_role.router.id
  policy = data.aws_iam_policy_document.telemetry.json
}

resource "aws_iam_instance_profile" "router" {
  name = "netcortex-lab-onprem-router"
  role = aws_iam_role.router.name
}

# --- the router ---

resource "aws_instance" "router" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  iam_instance_profile        = aws_iam_instance_profile.router.name
  user_data                   = local.user_data
  user_data_replace_on_change = true

  primary_network_interface {
    network_interface_id = aws_network_interface.wan.id
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = { Name = "netcortex-lab-onprem-router" }

  # The check is set on the WAN ENI (off). The instance attribute can't be set
  # alongside primary_network_interface and defaults to true, so every plan
  # would try to turn it back on.
  lifecycle {
    ignore_changes = [source_dest_check]
  }

  depends_on = [aws_eip_association.wan, aws_iam_role_policy.psk, aws_iam_role_policy.telemetry]
}

resource "aws_network_interface_attachment" "lan" {
  instance_id          = aws_instance.router.id
  network_interface_id = aws_network_interface.lan.id
  device_index         = 1
}
