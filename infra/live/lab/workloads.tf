# Workloads and synthetic traffic (spec §3, Stage 1 PR 3). Every flow reports to
# CloudWatch namespace NetCortexLab/Traffic, dimension Flow:
#   onprem-to-nlb        client -> NLB -> app-a/b, every 2s (crosses the VPN)
#   onprem-to-nlb-large  client -> NLB /large.bin (256 KiB), every 10s (MTU/MSS canary)
#   onprem-iperf-app-a   client -> app-a, UDP 2 Mbps for 10s every minute (loss/jitter)
#   svc-to-app           svc -> app.lab.internal -> NLB, every 5s (east-west, DNS-dependent)

locals {
  traffic_namespace = "NetCortexLab/Traffic"
  shared_cidr       = module.shared_vpc.cidr_block
  app_cidr          = module.app_vpc.cidr_block
  nlb_url           = "http://${aws_lb.app.dns_name}"
}

# --- IAM: SSM plus PutMetricData to the traffic namespace only ---

resource "aws_iam_role" "workload" {
  name               = "netcortex-lab-workload"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "workload_ssm" {
  role       = aws_iam_role.workload.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "workload_metrics" {
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.traffic_namespace]
    }
  }
}

resource "aws_iam_role_policy" "workload_metrics" {
  name   = "put-traffic-metrics"
  role   = aws_iam_role.workload.id
  policy = data.aws_iam_policy_document.workload_metrics.json
}

resource "aws_iam_instance_profile" "workload" {
  name = "netcortex-lab-workload"
  role = aws_iam_role.workload.name
}

# --- security groups ---
# Inbound rules are separate resources, one per source, so a fault can revoke
# exactly one (e.g. "on-prem clients") and leave the NLB health checks green.

resource "aws_security_group" "app_web" {
  name        = "netcortex-lab-app-web"
  description = "App web servers: HTTP from clients (source IP preserved by the NLB) and NLB health checks"
  vpc_id      = module.app_vpc.vpc_id

  tags = { Name = "netcortex-lab-app-web" }
}

resource "aws_vpc_security_group_ingress_rule" "app_http_onprem" {
  security_group_id = aws_security_group.app_web.id
  description       = "HTTP from on-prem clients"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = local.onprem_cidr
}

resource "aws_vpc_security_group_ingress_rule" "app_http_shared" {
  security_group_id = aws_security_group.app_web.id
  description       = "HTTP from the shared VPC (svc)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = local.shared_cidr
}

resource "aws_vpc_security_group_ingress_rule" "app_http_nlb" {
  security_group_id = aws_security_group.app_web.id
  description       = "HTTP health checks from the NLB nodes in the app VPC"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = local.app_cidr
}

resource "aws_vpc_security_group_ingress_rule" "app_iperf_onprem" {
  for_each = toset(["tcp", "udp"])

  security_group_id = aws_security_group.app_web.id
  description       = "iperf3 ${each.key} from on-prem clients"
  ip_protocol       = each.key
  from_port         = 5201
  to_port           = 5201
  cidr_ipv4         = local.onprem_cidr
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app_web.id
  description       = "All outbound (replies, packages, SSM, metrics)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# Traffic sources need no inbound rules at all (spec D5: SSM only).
resource "aws_security_group" "traffic_source" {
  for_each = {
    svc    = module.shared_vpc.vpc_id
    client = module.onprem_vpc.vpc_id
  }

  name        = "netcortex-lab-${each.key}"
  description = "Egress-only: ${each.key} generates traffic, accepts none"
  vpc_id      = each.value

  tags = { Name = "netcortex-lab-${each.key}" }
}

resource "aws_vpc_security_group_egress_rule" "traffic_source_all" {
  for_each = aws_security_group.traffic_source

  security_group_id = each.value.id
  description       = "All outbound (flows, packages, SSM, metrics)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# --- internal NLB in front of app-a and app-b ---

resource "aws_lb" "app" {
  name               = "netcortex-lab-app"
  internal           = true
  load_balancer_type = "network"
  subnets            = module.app_vpc.subnet_ids_by_role["private"]

  tags = { Name = "netcortex-lab-app" }
}

# Instance targets keep the client IP, so app SGs see 172.16/16 and 10.20/16.
resource "aws_lb_target_group" "app" {
  name                 = "netcortex-lab-app"
  port                 = 80
  protocol             = "TCP"
  target_type          = "instance"
  vpc_id               = module.app_vpc.vpc_id
  deregistration_delay = 10

  health_check {
    protocol            = "HTTP"
    path                = "/health"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = { Name = "netcortex-lab-app" }
}

resource "aws_lb_listener" "app" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_lb_target_group_attachment" "app" {
  for_each = module.app

  target_group_arn = aws_lb_target_group.app.arn
  target_id        = each.value.instance_id
  port             = 80
}

# --- instances ---

module "app" {
  source   = "../../modules/workload"
  for_each = { a = 0, b = 1 }

  name                  = "app-${each.key}"
  region                = var.region
  ami_id                = data.aws_ssm_parameter.ubuntu_arm64.insecure_value
  subnet_id             = module.app_vpc.subnet_ids["private-${each.key}"]
  security_group_ids    = [aws_security_group.app_web.id]
  instance_profile_name = aws_iam_instance_profile.workload.name
  web                   = true
  iperf_server          = each.key == "a"

  # Package install needs the egress path (TGW -> NAT) in place.
  depends_on = [
    aws_route.spoke_default,
    aws_ec2_transit_gateway_route.spoke_default,
    aws_ec2_transit_gateway_route_table_association.spoke,
  ]
}

module "svc" {
  source = "../../modules/workload"

  name                  = "svc"
  region                = var.region
  ami_id                = data.aws_ssm_parameter.ubuntu_arm64.insecure_value
  subnet_id             = module.shared_vpc.subnet_ids["private-a"]
  security_group_ids    = [aws_security_group.traffic_source["svc"].id]
  instance_profile_name = aws_iam_instance_profile.workload.name
  metric_namespace      = local.traffic_namespace
  flows = [
    { name = "svc-to-app", kind = "http", target = "http://app.${var.dns_zone_name}/", interval = 5 },
  ]

  depends_on = [
    aws_route.spoke_default,
    aws_ec2_transit_gateway_route.spoke_default,
    aws_ec2_transit_gateway_route_table_association.spoke,
  ]
}

module "client" {
  source = "../../modules/workload"

  name                  = "client"
  region                = var.region
  ami_id                = data.aws_ssm_parameter.ubuntu_arm64.insecure_value
  subnet_id             = module.onprem_vpc.subnet_ids["lan"]
  security_group_ids    = [aws_security_group.traffic_source["client"].id]
  instance_profile_name = aws_iam_instance_profile.workload.name
  metric_namespace      = local.traffic_namespace
  flows = [
    { name = "onprem-to-nlb", kind = "http", target = "${local.nlb_url}/", interval = 2 },
    { name = "onprem-to-nlb-large", kind = "http", target = "${local.nlb_url}/large.bin", interval = 10, timeout = 10 },
    { name = "onprem-iperf-app-a", kind = "iperf", target = module.app["a"].private_ip, interval = 60, bitrate_mbps = 2, duration = 10 },
  ]

  # Egress is via the router's MASQUERADE; workload-up retries until it is ready.
  depends_on = [aws_route.onprem_lan_default]
}
