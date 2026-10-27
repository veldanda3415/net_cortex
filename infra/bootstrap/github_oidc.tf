# AWS validates GitHub's OIDC certificates against its own trusted CA library,
# so no thumbprint pinning is needed (thumbprint_list is optional in provider 6.x).
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "gha_plan_trust" {
  statement {
    sid     = "GitHubActionsPlan"
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

    # Exact matches only: pull requests from this repo, and pushes/dispatches on main.
    # Jobs that set `environment:` get a different sub and are rejected by design.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repository}:pull_request",
        "repo:${var.github_repository}:ref:refs/heads/main",
      ]
    }
  }
}

resource "aws_iam_role" "gha_plan" {
  name                 = "netcortex-gha-plan"
  description          = "GitHub Actions terraform plan role - read-only infra plus state bucket access"
  assume_role_policy   = data.aws_iam_policy_document.gha_plan_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "gha_plan_readonly" {
  role       = aws_iam_role.gha_plan.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "gha_plan_state" {
  statement {
    sid       = "StateBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.tfstate.arn]
  }

  # DeleteObject is required: S3 native locking removes the .tflock object on unlock.
  statement {
    sid    = "StateObjectsReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.tfstate.arn}/*"]
  }
}

resource "aws_iam_role_policy" "gha_plan_state" {
  name   = "terraform-state-bucket"
  role   = aws_iam_role.gha_plan.id
  policy = data.aws_iam_policy_document.gha_plan_state.json
}
