variable "zone_name" {
  description = "Private hosted zone name."
  type        = string
  default     = "lab.internal"
}

variable "vpc_ids" {
  description = "VPCs the zone is associated with, keyed by VPC name."
  type        = map(string)
}

variable "alias_records" {
  description = "A-record aliases keyed by record name (relative to the zone), e.g. app -> the NLB."
  type = map(object({
    dns_name = string
    zone_id  = string
  }))
  default = {}
}

variable "a_records" {
  description = "Plain A records keyed by record name, each a list of IPv4 addresses."
  type        = map(list(string))
  default     = {}
}

variable "query_log_vpc_ids" {
  description = "VPCs whose Resolver queries are logged to CloudWatch Logs, keyed by VPC name."
  type        = map(string)
}

variable "retention_days" {
  description = "Retention for Resolver query logs."
  type        = number
  default     = 3
}
