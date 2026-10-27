# One stateful Linux firewall per AZ (spec D4). The security VPC's TGW subnets
# route 0/0 to the firewall ENI in the same AZ; appliance mode on the security
# attachment keeps both directions of a flow on the same firewall.

resource "aws_cloudwatch_log_group" "drops" {
  name              = "/netcortex-lab/firewall/drops"
  retention_in_days = var.log_retention_days
}

# --- IAM: SSM, drop logs and metrics only ---

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "firewall" {
  name               = "netcortex-lab-firewall"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.firewall.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "telemetry" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = [aws_cloudwatch_log_group.drops.arn, "${aws_cloudwatch_log_group.drops.arn}:*"]
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
  name   = "firewall-telemetry"
  role   = aws_iam_role.firewall.id
  policy = data.aws_iam_policy_document.telemetry.json
}

resource "aws_iam_instance_profile" "firewall" {
  name = "netcortex-lab-firewall"
  role = aws_iam_role.firewall.name
}

# --- network: the SG admits all lab traffic; the iptables ruleset does the filtering ---

resource "aws_security_group" "firewall" {
  name        = "netcortex-lab-firewall"
  description = "Inspection firewalls: everything from lab ranges, filtered by iptables"
  vpc_id      = var.vpc_id

  tags = { Name = "netcortex-lab-firewall" }
}

resource "aws_vpc_security_group_ingress_rule" "lab" {
  for_each = toset([var.cidrs.cloud, var.cidrs.onprem])

  security_group_id = aws_security_group.firewall.id
  description       = "All traffic from ${each.key} (filtered on the host)"
  ip_protocol       = "-1"
  cidr_ipv4         = each.key
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.firewall.id
  description       = "All outbound (forwarded flows, packages, SSM, telemetry)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# A firewall forwards, so the source/dest check is off on its ENI.
resource "aws_network_interface" "this" {
  for_each = var.subnet_ids

  subnet_id         = each.value
  security_groups   = [aws_security_group.firewall.id]
  source_dest_check = false

  tags = { Name = "netcortex-lab-fw-${each.key}" }
}

resource "aws_instance" "this" {
  for_each = var.subnet_ids

  ami                         = var.ami_id
  instance_type               = var.instance_type
  iam_instance_profile        = aws_iam_instance_profile.firewall.name
  user_data_replace_on_change = true

  user_data = "#cloud-config\n${yamlencode({
    hostname       = "fw-${each.key}"
    package_update = true
    packages       = ["jq", "iptables"]
    snap           = { commands = ["snap install aws-cli --classic"] }
    write_files = [
      {
        path        = "/etc/netcortex/firewall.json"
        permissions = "0644"
        content = jsonencode({
          name             = "fw-${each.key}"
          region           = var.region
          cidrs            = var.cidrs
          log_group        = aws_cloudwatch_log_group.drops.name
          metric_namespace = var.metric_namespace
        })
      },
      { path = "/usr/local/sbin/netcortex-firewall-up", permissions = "0755", content = file("${path.module}/firewall-up.sh") },
      { path = "/usr/local/sbin/netcortex-fw-metrics", permissions = "0755", content = file("${path.module}/fw-metrics.sh") },
      { path = "/etc/systemd/system/netcortex-firewall.service", permissions = "0644", content = file("${path.module}/netcortex-firewall.service") },
      { path = "/etc/systemd/system/netcortex-fw-metrics.service", permissions = "0644", content = file("${path.module}/netcortex-fw-metrics.service") },
      { path = "/etc/systemd/system/netcortex-fw-metrics.timer", permissions = "0644", content = file("${path.module}/netcortex-fw-metrics.timer") },
    ]
    runcmd = [
      ["systemctl", "daemon-reload"],
      ["systemctl", "enable", "--now", "--no-block", "netcortex-firewall.service"],
    ]
  })}"

  primary_network_interface {
    network_interface_id = aws_network_interface.this[each.key].id
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

  tags = { Name = "netcortex-lab-fw-${each.key}" }

  # Same as the router: the check lives on the ENI, and the instance attribute
  # can't be set alongside primary_network_interface.
  lifecycle {
    ignore_changes = [source_dest_check]
  }

  depends_on = [aws_iam_role_policy.telemetry, aws_iam_role_policy_attachment.ssm]
}
