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
  description = "Route spoke and on-prem traffic through the security VPC firewalls (spec D3)."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_inspection
    error_message = "Inspection firewalls are not built yet (Stage 1 PR 5); enable_inspection must stay false."
  }
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
