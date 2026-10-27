variable "name" {
  description = "Short VPC name, used in Name tags (e.g. app, shared, security)."
  type        = string
}

variable "cidr_block" {
  description = "VPC IPv4 CIDR."
  type        = string
}

variable "subnets" {
  description = "Subnets keyed by short name (e.g. private-a). az is an AZ name; role groups subnets for callers (private, tgw, public, fw)."
  type = map(object({
    cidr = string
    az   = string
    role = string
  }))
}
