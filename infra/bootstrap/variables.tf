variable "region" {
  description = "Region for the state bucket and all bootstrap resources."
  type        = string
  default     = "us-east-1"
}

variable "owner" {
  description = "Value of the Owner default tag."
  type        = string
  default     = "ravi"
}

variable "alert_email" {
  description = "Email address that receives AWS Budget notifications."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be a valid email address."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget in USD."
  type        = number
  default     = 50
}

variable "github_repository" {
  description = "GitHub repository (owner/name) allowed to assume the plan role."
  type        = string
  default     = "veldanda3415/net_cortex"
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent state object versions are deleted."
  type        = number
  default     = 30
}

variable "reaper_environment" {
  description = "GitHub environment the nightly reaper job runs in; the only identity the apply role trusts."
  type        = string
  default     = "lab-reaper"
}

variable "lab_regions" {
  description = "Regions the apply role may act in (us-west-2 hosts Network Manager events)."
  type        = list(string)
  default     = ["us-east-1", "us-west-2"]
}
