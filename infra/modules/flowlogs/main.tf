locals {
  # Custom v5 format (spec §4): adds tcp-flags, pkt-src/dst, flow-direction and
  # traffic-path so the RCA can tell NAT/TGW hops and asymmetric paths apart.
  flow_log_fields = [
    "version", "account-id", "interface-id", "srcaddr", "dstaddr", "srcport", "dstport",
    "protocol", "packets", "bytes", "start", "end", "action", "log-status",
    "vpc-id", "subnet-id", "instance-id", "tcp-flags", "type",
    "pkt-srcaddr", "pkt-dstaddr", "region", "az-id",
    "pkt-src-aws-service", "pkt-dst-aws-service", "flow-direction", "traffic-path",
  ]
  flow_log_format = join(" ", [for f in local.flow_log_fields : "$${${f}}"])
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "flowlogs" {
  name               = "netcortex-lab-flowlogs"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_cloudwatch_log_group" "this" {
  for_each = var.vpcs

  name              = "/netcortex-lab/vpc-flow/${each.key}"
  retention_in_days = var.retention_days
}

data "aws_iam_policy_document" "write" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = flatten([for lg in aws_cloudwatch_log_group.this : [lg.arn, "${lg.arn}:*"]])
  }
}

resource "aws_iam_role_policy" "write" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flowlogs.id
  policy = data.aws_iam_policy_document.write.json
}

resource "aws_flow_log" "this" {
  for_each = var.vpcs

  vpc_id                   = each.value
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.this[each.key].arn
  iam_role_arn             = aws_iam_role.flowlogs.arn
  log_format               = local.flow_log_format
  max_aggregation_interval = 60

  tags = { Name = "netcortex-lab-${each.key}-flow" }

  depends_on = [aws_iam_role_policy.write]
}
