output "instance_id" {
  description = "Router instance ID."
  value       = aws_instance.router.id
}

output "lan_network_interface_id" {
  description = "Router LAN ENI; the on-prem LAN routes through it."
  value       = aws_network_interface.lan.id
}

output "lan_ip" {
  description = "Router LAN IP (on-prem hosts' next hop)."
  value       = aws_network_interface.lan.private_ip
}

output "wan_private_ip" {
  description = "Router WAN private IP (VTI local address)."
  value       = aws_network_interface.wan.private_ip
}
