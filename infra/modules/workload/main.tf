locals {
  has_iperf_flow = anytrue([for f in var.flows : f.kind == "iperf"])

  packages = concat(
    var.web ? ["nginx"] : [],
    var.iperf_server || local.has_iperf_flow ? ["iperf3"] : [],
    length(var.flows) > 0 ? ["python3-boto3"] : [],
  )

  workload_config = {
    name         = var.name
    region       = var.region
    namespace    = var.metric_namespace
    packages     = local.packages
    web          = var.web
    iperf_server = var.iperf_server
    flows        = var.flows
  }

  # Packages are installed by workload-up (with retries), not cloud-init: the
  # on-prem client's only way out is the router, which may still be booting.
  user_data = "#cloud-config\n${yamlencode({
    hostname = var.name
    write_files = [
      { path = "/etc/netcortex/workload.json", permissions = "0644", content = jsonencode(local.workload_config) },
      { path = "/usr/local/sbin/netcortex-workload-up", permissions = "0755", content = file("${path.module}/workload-up.sh") },
      { path = "/usr/local/bin/netcortex-traffic", permissions = "0755", content = file("${path.module}/traffic.py") },
      { path = "/etc/systemd/system/netcortex-workload.service", permissions = "0644", content = file("${path.module}/netcortex-workload.service") },
      { path = "/etc/systemd/system/netcortex-traffic.service", permissions = "0644", content = file("${path.module}/netcortex-traffic.service") },
    ]
    runcmd = [
      ["systemctl", "daemon-reload"],
      ["systemctl", "enable", "--now", "--no-block", "netcortex-workload.service"],
    ]
  })}"
}

resource "aws_instance" "this" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = var.security_group_ids
  iam_instance_profile        = var.instance_profile_name
  user_data                   = local.user_data
  user_data_replace_on_change = true

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = { Name = "netcortex-lab-${var.name}" }
}
