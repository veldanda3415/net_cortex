variable "cidr_block" {
  description = "Security VPC CIDR."
  type        = string
  default     = "10.30.0.0/16"
}

variable "az_names" {
  description = "Two AZ names, in a/b order."
  type        = list(string)

  validation {
    condition     = length(var.az_names) == 2
    error_message = "az_names must contain exactly two AZs (a, b)."
  }
}

variable "transit_gateway_id" {
  description = "TGW to attach to."
  type        = string
}

variable "return_cidrs" {
  description = "Destinations reached back through the TGW (cloud summary plus on-prem)."
  type        = list(string)
  default     = ["10.0.0.0/8", "172.16.0.0/16"]
}
