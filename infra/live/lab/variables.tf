# Every variable has a default so the nightly reaper can destroy without a tfvars file.

variable "region" {
  description = "Lab region."
  type        = string
  default     = "us-east-1"
}

variable "owner" {
  description = "Value of the Owner default tag."
  type        = string
  default     = "ravi"
}

variable "az_ids" {
  description = "AZ IDs (not names) for the a/b subnets; names differ per account, IDs do not (spec D8)."
  type        = list(string)
  default     = ["use1-az1", "use1-az2"]

  validation {
    condition     = length(var.az_ids) == 2
    error_message = "az_ids must contain exactly two AZ IDs (a, b)."
  }
}

variable "enable_inspection" {
  description = "Build the security VPC firewalls and route spoke and on-prem traffic through them (spec D3)."
  type        = bool
  default     = false
}

variable "flow_log_retention_days" {
  description = "Retention for VPC Flow Logs in CloudWatch Logs."
  type        = number
  default     = 3
}

variable "enable_test_instance" {
  description = "Create a temporary SSM-only instance in app private-a for the egress exit test."
  type        = bool
  default     = false
}

variable "onprem_asn" {
  description = "BGP ASN of the on-prem router / customer gateway."
  type        = number
  default     = 65010
}

variable "tunnel_inside_cidrs" {
  description = "VPN tunnel inside /30s (outside the AWS-reserved 169.254.0-5.0/30 and 169.254.169.252/30)."
  type        = list(string)
  default     = ["169.254.100.0/30", "169.254.100.4/30"]
}

variable "router_instance_type" {
  description = "On-prem router instance type."
  type        = string
  default     = "t4g.small"
}

variable "dns_zone_name" {
  description = "Private hosted zone for the lab (app.<zone> aliases the NLB)."
  type        = string
  default     = "lab.internal"
}

variable "dns_query_log_retention_days" {
  description = "Retention for Route 53 Resolver query logs in CloudWatch Logs."
  type        = number
  default     = 3
}

variable "enable_peering" {
  description = "Build the partner VPC, peered with app only (the non-transitivity check)."
  type        = bool
  default     = true
}

variable "vpn_mode" {
  description = <<-EOT
    bgp_active_standby: tunnel 1 preferred both ways (AS-path prepend out, local-pref in on tunnel 2).
    bgp_ecmp: both tunnels equal, traffic spread across both.
    static: no BGP; static routes on both sides. Recreates the VPN (~5-10 min, new tunnel IPs).
  EOT
  type        = string
  default     = "bgp_active_standby"

  validation {
    condition     = contains(["bgp_active_standby", "bgp_ecmp", "static"], var.vpn_mode)
    error_message = "vpn_mode must be bgp_active_standby, bgp_ecmp or static."
  }
}
