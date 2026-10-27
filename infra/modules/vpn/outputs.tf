output "vpn_connection_id" {
  description = "Site-to-Site VPN connection ID."
  value       = aws_vpn_connection.onprem.id
}

output "transit_gateway_attachment_id" {
  description = "TGW attachment created for the VPN."
  value       = aws_vpn_connection.onprem.transit_gateway_attachment_id
}

output "tunnel_outside_ips" {
  description = "AWS tunnel endpoint public IPs, tunnel 1/2 order."
  value       = [aws_vpn_connection.onprem.tunnel1_address, aws_vpn_connection.onprem.tunnel2_address]
}

output "psk_parameter_names" {
  description = "Parameter Store names of the PSKs, tunnel 1/2 order."
  value       = [aws_ssm_parameter.psk["1"].name, aws_ssm_parameter.psk["2"].name]
}

output "psk_parameter_arns" {
  description = "Parameter Store ARNs of the PSKs."
  value       = [for p in aws_ssm_parameter.psk : p.arn]
}

output "log_group_names" {
  description = "Tunnel activity and BGP log groups."
  value = {
    tunnel = aws_cloudwatch_log_group.tunnel.name
    bgp    = aws_cloudwatch_log_group.bgp.name
  }
}
