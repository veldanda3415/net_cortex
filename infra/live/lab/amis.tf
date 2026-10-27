# Ubuntu 24.04 arm64 (Graviton, spec D7), resolved from Canonical's public parameter.
data "aws_ssm_parameter" "ubuntu_arm64" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}
