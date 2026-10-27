# Network Manager events (spec §4): TGW route installed/uninstalled and VPN
# IPsec/BGP up/down, as discrete events with timestamps. Network Manager only
# emits them for a TGW registered in a global network, and only in its home
# region (us-west-2), so a rule there forwards them to this region's default bus,
# where a second rule writes them to CloudWatch Logs next to everything else.
# Network Manager itself is free; only the forwarded events are billed (cents).

data "aws_caller_identity" "current" {}

locals {
  network_manager_events_pattern = jsonencode({ source = ["aws.networkmanager"] })
  home_event_bus_arn             = "arn:aws:events:${var.region}:${data.aws_caller_identity.current.account_id}:event-bus/default"
}

resource "aws_networkmanager_global_network" "lab" {
  provider = aws.network_manager_home

  description = "netcortex-lab"

  tags = { Name = "netcortex-lab" }
}

resource "aws_networkmanager_transit_gateway_registration" "core" {
  provider = aws.network_manager_home

  global_network_id   = aws_networkmanager_global_network.lab.id
  transit_gateway_arn = module.tgw.arn
}

# --- us-west-2: forward every Network Manager event to the lab region ---

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "events_forward" {
  name               = "netcortex-lab-networkmanager-forward"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
}

data "aws_iam_policy_document" "events_forward" {
  statement {
    actions   = ["events:PutEvents"]
    resources = [local.home_event_bus_arn]
  }
}

resource "aws_iam_role_policy" "events_forward" {
  name   = "put-to-lab-region-bus"
  role   = aws_iam_role.events_forward.id
  policy = data.aws_iam_policy_document.events_forward.json
}

resource "aws_cloudwatch_event_rule" "network_manager_forward" {
  provider = aws.network_manager_home

  name          = "netcortex-lab-networkmanager-forward"
  description   = "Forward Network Manager events to the ${var.region} default bus"
  event_pattern = local.network_manager_events_pattern
}

resource "aws_cloudwatch_event_target" "network_manager_forward" {
  provider = aws.network_manager_home

  rule     = aws_cloudwatch_event_rule.network_manager_forward.name
  arn      = local.home_event_bus_arn
  role_arn = aws_iam_role.events_forward.arn
}

# --- lab region: write the forwarded events to CloudWatch Logs ---

resource "aws_cloudwatch_log_group" "network_manager_events" {
  name              = "/netcortex-lab/networkmanager/events"
  retention_in_days = var.flow_log_retention_days
}

# EventBridge writes to log groups through a resource policy, not a role.
data "aws_iam_policy_document" "network_manager_events_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.network_manager_events.arn}:*"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "network_manager_events" {
  policy_name     = "netcortex-lab-networkmanager-events"
  policy_document = data.aws_iam_policy_document.network_manager_events_logs.json
}

resource "aws_cloudwatch_event_rule" "network_manager_logs" {
  name          = "netcortex-lab-networkmanager-logs"
  description   = "Network Manager events forwarded from us-west-2, to CloudWatch Logs"
  event_pattern = local.network_manager_events_pattern
}

resource "aws_cloudwatch_event_target" "network_manager_logs" {
  rule = aws_cloudwatch_event_rule.network_manager_logs.name
  arn  = aws_cloudwatch_log_group.network_manager_events.arn

  depends_on = [aws_cloudwatch_log_resource_policy.network_manager_events]
}
