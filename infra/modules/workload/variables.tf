variable "name" {
  description = "Short workload name, used in Name tags and as the hostname (e.g. app-a, svc, client)."
  type        = string
}

variable "region" {
  description = "Region the traffic agent publishes metrics to."
  type        = string
}

variable "ami_id" {
  description = "Ubuntu 24.04 arm64 AMI."
  type        = string
}

variable "instance_type" {
  description = "Instance type."
  type        = string
  default     = "t4g.nano"
}

variable "subnet_id" {
  description = "Subnet to launch into."
  type        = string
}

variable "security_group_ids" {
  description = "Security groups for the instance."
  type        = list(string)
}

variable "instance_profile_name" {
  description = "Instance profile (SSM plus PutMetricData to the traffic namespace)."
  type        = string
}

variable "web" {
  description = "Run nginx on :80 with /health, / (hostname) and /large.bin (256 KiB, for MTU/MSS faults)."
  type        = bool
  default     = false
}

variable "iperf_server" {
  description = "Run an iperf3 server on :5201."
  type        = bool
  default     = false
}

variable "flows" {
  description = <<-EOT
    Synthetic flows this instance generates. Each is reported to CloudWatch under its name (dimension Flow).
    kind = "http": GET target (a URL) every interval seconds.
    kind = "iperf": UDP iperf3 to target (a host) at bitrate_mbps for duration seconds, every interval seconds.
  EOT
  type = list(object({
    name         = string
    kind         = string
    target       = string
    interval     = number
    timeout      = optional(number, 5)
    bitrate_mbps = optional(number, 2)
    duration     = optional(number, 10)
  }))
  default = []

  validation {
    condition     = alltrue([for f in var.flows : contains(["http", "iperf"], f.kind)])
    error_message = "flow kind must be http or iperf."
  }
}

variable "metric_namespace" {
  description = "CloudWatch namespace for traffic metrics."
  type        = string
  default     = "NetCortexLab/Traffic"
}
