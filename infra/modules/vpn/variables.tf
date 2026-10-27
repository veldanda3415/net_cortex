variable "transit_gateway_id" {
  description = "TGW the VPN attaches to."
  type        = string
}

variable "customer_gateway_ip" {
  description = "Public IP of the on-prem router (its Elastic IP)."
  type        = string
}

variable "onprem_asn" {
  description = "Customer gateway BGP ASN."
  type        = number
}

variable "tunnel_inside_cidrs" {
  description = "Inside /30s for tunnel 1 and 2 (AWS takes .1/.5, the router .2/.6)."
  type        = list(string)

  validation {
    condition     = length(var.tunnel_inside_cidrs) == 2
    error_message = "Exactly two tunnel inside CIDRs are required."
  }
}

variable "log_retention_days" {
  description = "Retention for tunnel activity and BGP logs."
  type        = number
  default     = 3
}
