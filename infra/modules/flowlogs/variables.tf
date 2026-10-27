variable "vpcs" {
  description = "VPC IDs keyed by short VPC name."
  type        = map(string)
}

variable "retention_days" {
  description = "CloudWatch Logs retention for flow logs."
  type        = number
  default     = 3
}
