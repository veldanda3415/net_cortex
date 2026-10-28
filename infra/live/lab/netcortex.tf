# NetCortex's connection to the lab (Phase 3): alarms in, telemetry out.
#
# Alarms: every lab alarm's change to ALARM goes from EventBridge to an SQS queue.
# NetCortex runs off-cloud (laptop or local cluster), so it polls the queue
# (`python app/main.py alarm-bridge`) instead of exposing a public webhook.
#
# Telemetry: one read-only role for the telemetry server and the bridge. It can read
# the lab's metrics, logs, CloudTrail and TGW routes, and is explicitly denied the
# faults log group, the eval answer key.

data "aws_iam_policy_document" "alarm_queue" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.alarms.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.alarms.arn]
    }
  }
}

resource "aws_sqs_queue" "alarms" {
  name                       = "netcortex-lab-alarms"
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20
  visibility_timeout_seconds = 900 # longer than an alarm burst plus one RCA
}

resource "aws_sqs_queue_policy" "alarms" {
  queue_url = aws_sqs_queue.alarms.id
  policy    = data.aws_iam_policy_document.alarm_queue.json
}

resource "aws_cloudwatch_event_rule" "alarms" {
  name        = "netcortex-lab-alarms"
  description = "Lab alarms going into ALARM, for NetCortex"
  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [{ prefix = "netcortex-lab-" }]
      state     = { value = ["ALARM"] }
    }
  })
}

resource "aws_cloudwatch_event_target" "alarms" {
  rule = aws_cloudwatch_event_rule.alarms.name
  arn  = aws_sqs_queue.alarms.arn

  depends_on = [aws_sqs_queue_policy.alarms]
}

# --- read-only role for NetCortex ---

# Any principal in this account allowed to assume roles (the admin SSO role) can use
# it, so a ~/.aws/config profile with role_arn + source_profile is all it takes.
data "aws_iam_policy_document" "netcortex_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

locals {
  lab_log_groups_arn = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/netcortex-lab/*"
}

data "aws_iam_policy_document" "netcortex_read" {
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }

  statement {
    sid       = "LabLogs"
    actions   = ["logs:FilterLogEvents", "logs:DescribeLogStreams"]
    resources = [local.lab_log_groups_arn, "${local.lab_log_groups_arn}:*"]
  }

  statement {
    sid       = "ManagementEvents"
    actions   = ["cloudtrail:LookupEvents"]
    resources = ["*"]
  }

  statement {
    sid       = "TgwRoutes"
    actions   = ["ec2:DescribeTransitGatewayRouteTables", "ec2:SearchTransitGatewayRoutes"]
    resources = ["*"]
  }

  statement {
    sid       = "AlarmQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.alarms.arn]
  }

  # Explicit deny wins over the LabLogs allow: NetCortex must never read the answer key.
  statement {
    sid       = "NeverTheAnswerKey"
    effect    = "Deny"
    actions   = ["logs:*"]
    resources = [aws_cloudwatch_log_group.faults.arn, "${aws_cloudwatch_log_group.faults.arn}:*"]
  }
}

resource "aws_iam_role" "netcortex" {
  name                 = "netcortex-lab-netcortex"
  description          = "NetCortex read-only access to lab telemetry and the alarm queue"
  assume_role_policy   = data.aws_iam_policy_document.netcortex_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "netcortex" {
  name   = "read-lab-telemetry"
  role   = aws_iam_role.netcortex.id
  policy = data.aws_iam_policy_document.netcortex_read.json
}

output "alarm_queue_url" {
  description = "SQS queue of lab alarms for `python app/main.py alarm-bridge --queue-url`."
  value       = aws_sqs_queue.alarms.id
}

output "netcortex_role_arn" {
  description = "Read-only role for NetCortex's telemetry server and alarm bridge (role_arn of the netcortex-lab profile)."
  value       = aws_iam_role.netcortex.arn
}
