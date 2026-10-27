locals {
  tunnels = {
    "1" = { inside_cidr = var.tunnel_inside_cidrs[0] }
    "2" = { inside_cidr = var.tunnel_inside_cidrs[1] }
  }
}

# AWS PSK rules: 8-64 chars of [A-Za-z0-9._], must not start with 0.
# The "nc" prefix guarantees a non-zero first character.
resource "random_password" "psk" {
  for_each = local.tunnels

  length  = 30
  special = false
}

# The router reads these at boot. The PSKs are also in Terraform state (accepted:
# the state bucket is SSE-S3, TLS-only and access-restricted).
resource "aws_ssm_parameter" "psk" {
  for_each = local.tunnels

  name        = "/netcortex-lab/vpn/tunnel${each.key}-psk"
  description = "Site-to-Site VPN tunnel ${each.key} pre-shared key"
  type        = "SecureString"
  tier        = "Standard"
  value       = "nc${random_password.psk[each.key].result}"
}

resource "aws_customer_gateway" "onprem" {
  bgp_asn    = var.onprem_asn
  ip_address = var.customer_gateway_ip
  type       = "ipsec.1"

  tags = { Name = "netcortex-lab-onprem-cgw" }
}

resource "aws_cloudwatch_log_group" "tunnel" {
  name              = "/netcortex-lab/vpn/tunnel-activity"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "bgp" {
  name              = "/netcortex-lab/vpn/bgp"
  retention_in_days = var.log_retention_days
}

resource "aws_vpn_connection" "onprem" {
  transit_gateway_id  = var.transit_gateway_id
  customer_gateway_id = aws_customer_gateway.onprem.id
  type                = "ipsec.1"
  static_routes_only  = var.static_routes_only

  # --- tunnel 1 ---
  tunnel1_inside_cidr                  = local.tunnels["1"].inside_cidr
  tunnel1_preshared_key                = aws_ssm_parameter.psk["1"].value
  tunnel1_ike_versions                 = ["ikev2"]
  tunnel1_phase1_encryption_algorithms = ["AES256"]
  tunnel1_phase1_integrity_algorithms  = ["SHA2-256"]
  tunnel1_phase1_dh_group_numbers      = [20]
  tunnel1_phase2_encryption_algorithms = ["AES256"]
  tunnel1_phase2_integrity_algorithms  = ["SHA2-256"]
  tunnel1_phase2_dh_group_numbers      = [20]
  tunnel1_dpd_timeout_seconds          = 30
  tunnel1_dpd_timeout_action           = "restart"
  tunnel1_startup_action               = "start"

  tunnel1_log_options {
    cloudwatch_log_options {
      log_enabled           = true
      log_group_arn         = aws_cloudwatch_log_group.tunnel.arn
      log_output_format     = "json"
      bgp_log_enabled       = true
      bgp_log_group_arn     = aws_cloudwatch_log_group.bgp.arn
      bgp_log_output_format = "json"
    }
  }

  # --- tunnel 2 ---
  tunnel2_inside_cidr                  = local.tunnels["2"].inside_cidr
  tunnel2_preshared_key                = aws_ssm_parameter.psk["2"].value
  tunnel2_ike_versions                 = ["ikev2"]
  tunnel2_phase1_encryption_algorithms = ["AES256"]
  tunnel2_phase1_integrity_algorithms  = ["SHA2-256"]
  tunnel2_phase1_dh_group_numbers      = [20]
  tunnel2_phase2_encryption_algorithms = ["AES256"]
  tunnel2_phase2_integrity_algorithms  = ["SHA2-256"]
  tunnel2_phase2_dh_group_numbers      = [20]
  tunnel2_dpd_timeout_seconds          = 30
  tunnel2_dpd_timeout_action           = "restart"
  tunnel2_startup_action               = "start"

  tunnel2_log_options {
    cloudwatch_log_options {
      log_enabled           = true
      log_group_arn         = aws_cloudwatch_log_group.tunnel.arn
      log_output_format     = "json"
      bgp_log_enabled       = true
      bgp_log_group_arn     = aws_cloudwatch_log_group.bgp.arn
      bgp_log_output_format = "json"
    }
  }

  tags = { Name = "netcortex-lab-onprem-vpn" }
}
