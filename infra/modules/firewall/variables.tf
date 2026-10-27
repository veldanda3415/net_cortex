variable "region" {
  description = "Region for metrics and logs."
  type        = string
}

variable "vpc_id" {
  description = "Security VPC ID."
  type        = string
}

variable "subnet_ids" {
  description = "Firewall subnet per AZ, keyed by AZ letter (a, b)."
  type        = map(string)
}

variable "ami_id" {
  description = "Ubuntu 24.04 arm64 AMI."
  type        = string
}

variable "instance_type" {
  description = "Firewall instance type."
  type        = string
  default     = "t4g.nano"
}

variable "cidrs" {
  description = "Ranges the ruleset is written in terms of."
  type = object({
    cloud  = string
    app    = string
    shared = string
    onprem = string
  })
}

variable "metric_namespace" {
  description = "CloudWatch namespace for firewall metrics."
  type        = string
  default     = "NetCortexLab/Firewall"
}

variable "log_retention_days" {
  description = "Retention for the firewall drop log group."
  type        = number
  default     = 3
}
