# VPN tunnel option helpers for the IKE fault. Source after lib.sh.
VPN=$(t .vpn_connection_id)
TUNNEL_IP=$(t '.vpn_tunnel_outside_ips[0]')

tunnel_dh_groups() {
  aws ec2 describe-vpn-connections --vpn-connection-ids "$VPN" \
    --query "VpnConnections[0].Options.TunnelOptions[?OutsideIpAddress=='$TUNNEL_IP'] | [0].Phase1DHGroupNumbers[].Value" \
    --output text
}

# A VPN takes one modification at a time; wait for it to settle before and after.
wait_vpn_available() {
  local state
  for _ in $(seq 1 120); do
    state=$(aws ec2 describe-vpn-connections --vpn-connection-ids "$VPN" --query 'VpnConnections[0].State' --output text)
    [ "$state" = available ] && return 0
    say "VPN is $state, waiting"
    sleep 15
  done
  die "VPN never returned to available"
}

modify_tunnel() {
  wait_vpn_available
  aws ec2 modify-vpn-tunnel-options --vpn-connection-id "$VPN" \
    --vpn-tunnel-outside-ip-address "$TUNNEL_IP" --tunnel-options "$1" >/dev/null
  wait_vpn_available
}
