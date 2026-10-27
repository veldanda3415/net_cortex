output "state_bucket_name" {
  description = "S3 bucket holding Terraform state for all NetCortex roots."
  value       = aws_s3_bucket.tfstate.bucket
}

output "plan_role_arn" {
  description = "IAM role GitHub Actions assumes for terraform plan (set as repo variable AWS_PLAN_ROLE_ARN)."
  value       = aws_iam_role.gha_plan.arn
}

output "github_oidc_provider_arn" {
  description = "IAM OIDC provider for token.actions.githubusercontent.com."
  value       = aws_iam_openid_connect_provider.github.arn
}

output "apply_role_arn" {
  description = "IAM role the nightly reaper assumes (set as repo variable AWS_APPLY_ROLE_ARN)."
  value       = aws_iam_role.gha_apply.arn
}
