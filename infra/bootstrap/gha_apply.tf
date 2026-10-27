# Role for the nightly reaper (Stage 1 PR 1). Humans apply (spec D9); this role
# exists to destroy, so it has no IAM create rights. It trusts exactly one OIDC subject:
# jobs running in the lab-reaper GitHub environment, whose deployment-branch rule
# allows main only. The reaper workflow itself only triggers on schedule and
# workflow_dispatch, so together: scheduled or manual runs on main.

data "aws_iam_policy_document" "gha_apply_trust" {
  statement {
    sid     = "GitHubActionsReaper"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.reaper_environment}"]
    }
  }
}

resource "aws_iam_role" "gha_apply" {
  name                 = "netcortex-gha-apply"
  description          = "GitHub Actions nightly reaper - tears down the ephemeral lab root"
  assume_role_policy   = data.aws_iam_policy_document.gha_apply_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "gha_apply_readonly" {
  role       = aws_iam_role.gha_apply.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "gha_apply" {
  # Services the lab root (all Stage 1 PRs) creates.
  statement {
    sid    = "LabServices"
    effect = "Allow"
    actions = [
      "ec2:*",
      "elasticloadbalancing:*",
      "logs:*",
      "cloudwatch:*",
      "ssm:*",
      "route53:*",
      "route53resolver:*",
      "networkmanager:*",
      "events:*",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = var.lab_regions
    }
  }

  # IAM is teardown-only: create/attach/pass would let a compromised run grant
  # itself AdministratorAccess through a new netcortex-lab-* role.
  statement {
    sid    = "LabIamTeardown"
    effect = "Allow"
    actions = [
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:DeleteInstanceProfile",
      "iam:RemoveRoleFromInstanceProfile",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/netcortex-lab-*",
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:instance-profile/netcortex-lab-*",
    ]
  }

  statement {
    sid       = "StateBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.tfstate.arn]
  }

  statement {
    sid       = "StateObjectsReadWrite"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.tfstate.arn}/*"]
  }

  # ec2:* would otherwise include the account-level guardrails from §2.
  statement {
    sid    = "KeepAccountGuardrails"
    effect = "Deny"
    actions = [
      "ec2:DisableEbsEncryptionByDefault",
      "ec2:ModifyEbsDefaultKmsKeyId",
      "ec2:ResetEbsDefaultKmsKeyId",
      "ec2:ModifyInstanceMetadataDefaults",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_apply" {
  name   = "lab-teardown"
  role   = aws_iam_role.gha_apply.id
  policy = data.aws_iam_policy_document.gha_apply.json
}
