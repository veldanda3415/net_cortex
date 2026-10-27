variable "vpcs" {
  description = "VPC IDs keyed by short VPC name."
  type        = map(string)
}

variable "retention_days" {
  description = "CloudWatch Logs retention for flow logs."
  type        = number
  default     = 3
}

variable "transit_gateways" {
  description = "Transit gateway IDs keyed by short name; each gets its own TGW flow log."
  type        = map(string)
  default     = {}
}
