# Temporary instance for the PR 1 exit test: SSM in, curl out via TGW -> NAT.
# Off by default; enable with -var enable_test_instance=true and turn off afterwards.

data "aws_ssm_parameter" "ubuntu_arm64" {
  count = var.enable_test_instance ? 1 : 0

  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "test_instance" {
  count = var.enable_test_instance ? 1 : 0

  name               = "netcortex-lab-test-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "test_instance_ssm" {
  count = var.enable_test_instance ? 1 : 0

  role       = aws_iam_role.test_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "test_instance" {
  count = var.enable_test_instance ? 1 : 0

  name = "netcortex-lab-test-instance"
  role = aws_iam_role.test_instance[0].name
}

# No ingress at all (spec D5): Session Manager connects outbound from the agent.
resource "aws_security_group" "test_instance" {
  count = var.enable_test_instance ? 1 : 0

  name        = "netcortex-lab-test-instance"
  description = "Egress-only SG for the temporary SSM test instance"
  vpc_id      = module.app_vpc.vpc_id

  egress {
    description = "All outbound (SSM, package mirrors, curl test)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "netcortex-lab-test-instance" }
}

resource "aws_instance" "test" {
  count = var.enable_test_instance ? 1 : 0

  ami                    = data.aws_ssm_parameter.ubuntu_arm64[0].insecure_value
  instance_type          = "t4g.nano"
  subnet_id              = module.app_vpc.subnet_ids["private-a"]
  vpc_security_group_ids = [aws_security_group.test_instance[0].id]
  iam_instance_profile   = aws_iam_instance_profile.test_instance[0].name

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = { Name = "netcortex-lab-test" }

  # SSM registration needs the egress path to exist first.
  depends_on = [
    aws_route.spoke_default,
    aws_ec2_transit_gateway_route.spoke_default,
    aws_ec2_transit_gateway_route_table_association.spoke,
  ]
}
