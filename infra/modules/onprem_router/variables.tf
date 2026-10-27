variable "region" {
  description = "Region (the router reads its PSKs from Parameter Store here)."
  type        = string
}

variable "vpc_id" {
  description = "On-prem VPC ID."
  type        = string
}

variable "wan_subnet_id" {
  description = "Public WAN subnet (router eth0 + EIP)."
  type        = string
}

variable "lan_subnet_id" {
  description = "LAN subnet (router eth1)."
  type        = string
}

variable "lan_cidr" {
  description = "LAN subnet CIDR."
  type        = string
}

variable "onprem_cidr" {
  description = "On-prem summary advertised over BGP."
  type        = string
}

variable "eip_allocation_id" {
  description = "Elastic IP for the WAN interface; the customer gateway points at it."
  type        = string
}

variable "eip_public_ip" {
  description = "Public IP of that Elastic IP (the router's IKE identity)."
  type        = string
}

variable "ami_id" {
  description = "Ubuntu 24.04 arm64 AMI."
  type        = string
}

variable "instance_type" {
  description = "Router instance type."
  type        = string
  default     = "t4g.small"
}

variable "onprem_asn" {
  description = "Router BGP ASN."
  type        = number
}

variable "aws_asn" {
  description = "AWS-side (TGW) BGP ASN."
  type        = number
}

variable "tunnels" {
  description = "Per-tunnel settings, in tunnel 1/2 order."
  type = list(object({
    outside_ip      = string
    inside_cidr     = string
    psk_parameter   = string
    as_path_prepend = number
    local_pref      = number
  }))
}

variable "psk_parameter_arns" {
  description = "Parameter Store ARNs the router may read."
  type        = list(string)
}

variable "mtu" {
  description = "VTI MTU (AWS requires 1399 or less; no PMTUD on VPN)."
  type        = number
  default     = 1399
}

variable "mss" {
  description = "TCP MSS clamp on the tunnels."
  type        = number
  default     = 1359
}

variable "metric_namespace" {
  description = "CloudWatch namespace for the router's BGP metrics."
  type        = string
  default     = "NetCortexLab/Router"
}

variable "log_retention_days" {
  description = "Retention for the router's FRR and charon log groups."
  type        = number
  default     = 3
}

variable "vpn_mode" {
  description = "bgp_active_standby, bgp_ecmp or static (see the lab root's vpn_mode)."
  type        = string
  default     = "bgp_active_standby"
}

variable "cloud_cidr" {
  description = "Cloud summary routed into the tunnels in static mode."
  type        = string
  default     = "10.0.0.0/8"
}
