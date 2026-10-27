# infra/bootstrap

Persistent root for the NetCortex AWS lab (Stage 0a, §2 of
[stage1-network-design-spec.md](../docs/stage1-network-design-spec.md)). Costs roughly $0/month.

It creates:

| Resource | Details |
|---|---|
| S3 state bucket `netcortex-tfstate-<account_id>` | Versioning, SSE-S3, all public access blocked, deny non-TLS, noncurrent versions expire after 30 days, `prevent_destroy` |
| Account S3 Block Public Access | All four settings on for the whole account, so a future lab bucket can't be made public by mistake |
| AWS Budget `netcortex-monthly` | $50/month; email at 50/80/100% actual and 100% forecast |
| IAM OIDC provider | `token.actions.githubusercontent.com`, audience `sts.amazonaws.com` |
| IAM role `netcortex-gha-plan` | Trusted by `veldanda3415/net_cortex` pull requests and `main` only; `ReadOnlyAccess` plus read/write on the state bucket; 1-hour max session |

The apply role and the nightly reaper come in Stage 1 PR 1, not here.

This root is applied by a human only (spec D9). CI never applies it.

## Manual prerequisites (one time, console)

- [ ] Root user has MFA on and **no** access keys.
- [ ] IAM Identity Center is enabled, with an admin permission set assigned to you.
- [ ] Your CLI uses SSO (`aws sso login`), not long-lived keys. The examples below use the `aws-veldanda-admin` profile.
- [ ] EBS default encryption is on in us-east-1 (EC2 console → Settings → EBS encryption, or `aws ec2 enable-ebs-encryption-by-default`).
- [ ] IMDSv2 is required by default in us-east-1: `aws ec2 modify-instance-metadata-defaults --http-tokens required --http-put-response-hop-limit 2`.
- [ ] Cost Anomaly Detection has one monitor plus an email subscription (free).
- [ ] No GitHub OIDC provider exists yet in the account (IAM → Identity providers). If one does, import it instead of creating a second: `terraform import aws_iam_openid_connect_provider.github <arn>`.
- [ ] Terraform >= 1.10 and tflint are installed.

## Apply (local state)

```bash
cd infra/bootstrap
export AWS_PROFILE=aws-veldanda-admin
aws sso login --sso-session personal

cp terraform.tfvars.example terraform.tfvars   # set alert_email; the file is gitignored

terraform init
terraform fmt -check && terraform validate && tflint --init && tflint
terraform plan -out=tfplan
terraform apply tfplan
```

The first apply keeps state in `infra/bootstrap/terraform.tfstate` on your machine. Don't lose it before the next step.

## Migrate state into the bucket

Read the bucket name **before** creating `backend.tf`. Once that file exists, `terraform output` refuses to run until the backend is initialized. The substitution would then come back empty, and init would fail with "The value cannot be empty".

```bash
B=$(terraform output -raw state_bucket_name); echo "$B"
cp backend.tf.example backend.tf
terraform init -migrate-state -backend-config="bucket=$B"
# answer "yes" to copy the existing state to the new backend

terraform plan          # must report "No changes."
rm terraform.tfstate terraform.tfstate.backup
```

`backend.tf` is gitignored, because the bucket name contains the account ID. On a fresh clone, recreate it the same way and run `terraform init -backend-config="bucket=netcortex-tfstate-<account_id>"`.

## State key convention for downstream roots

One bucket, one key per root, split by lifecycle (spec D6):

| Root | Key |
|---|---|
| `infra/bootstrap` | `bootstrap/terraform.tfstate` |
| `infra/live/lab` | `lab/terraform.tfstate` |
| `infra/live/eks` (Stage 2) | `eks/terraform.tfstate` |

Every downstream root uses S3 native locking. There's no DynamoDB table:

```hcl
terraform {
  backend "s3" {
    key          = "lab/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # writes lab/terraform.tfstate.tflock next to the state
  }
}
```

Pass the bucket name with `-backend-config="bucket=..."` (locally from `terraform output -raw state_bucket_name`, in CI from a repo variable), so that no account ID is committed.

## Verify

After the apply, run these with your admin profile:

```bash
B=$(terraform output -raw state_bucket_name)
aws s3api get-bucket-versioning --bucket "$B"                    # Status: Enabled
aws s3api get-bucket-encryption --bucket "$B"                    # AES256
aws s3api get-public-access-block --bucket "$B"                  # all four true
aws s3control get-public-access-block \
  --account-id "$(aws sts get-caller-identity --query Account --output text)"   # all four true
aws s3api get-bucket-policy --bucket "$B" --query Policy --output text   # DenyNonTLS
aws s3api get-bucket-lifecycle-configuration --bucket "$B"       # NoncurrentDays: 30
aws budgets describe-notifications-for-budget \
  --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --budget-name netcortex-monthly                                # 4 notifications
aws iam get-role --role-name netcortex-gha-plan \
  --query 'Role.{Max:MaxSessionDuration,Trust:AssumeRolePolicyDocument}'
```

Then check GitHub OIDC end to end:

1. In the repo, go to Settings → Secrets and variables → Actions → Variables, and set `AWS_PLAN_ROLE_ARN` to `terraform output -raw plan_role_arn`.
2. Make sure `.github/workflows/aws-oidc-smoke.yml` is on `main`. GitHub only offers manual dispatch for workflows on the default branch, so the check can't run before this PR merges.
3. Go to Actions → aws-oidc-smoke → Run workflow, on `main`. The job summary shows the assumed role `netcortex-gha-plan`.
4. Optional negative test: dispatch from any other branch. STS should reject it with `Not authorized to perform sts:AssumeRoleWithWebIdentity`.

## Notes

- `ReadOnlyAccess` includes `s3:GetObject` on every bucket and `ssm:GetParameter` in the account. Together with read access to state, that means any PR job running as this role can read the VPN PSKs added in Stage 1 PR 2. That's accepted for a single-owner lab account. Fork PRs don't get OIDC tokens, so outside contributors can't use the role.
- Bootstrap resources are tagged `Ephemeral=false` (lab roots use `true`), so the nightly reaper can never target them.
- The state bucket has `prevent_destroy`. Tearing it down deliberately means removing that line first.
