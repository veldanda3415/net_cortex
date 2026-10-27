#!/bin/bash
# NetCortex on-prem router bring-up. Runs at every boot (netcortex-router.service).
# Reads non-secret config from /etc/netcortex/router.json and the tunnel PSKs from
# SSM Parameter Store, then (re)builds VTIs, strongSwan, FRR and iptables.
# PSKs never touch user_data, which is readable via DescribeInstanceAttribute.
set -euo pipefail

CFG=/etc/netcortex/router.json
AWS=/snap/bin/aws
j() { jq -r "$1" "$CFG"; }

REGION=$(j .region)
LOCAL_IP=$(j .wan_private_ip)
PUBLIC_IP=$(j .public_ip)
LAN_MAC=$(j .lan_mac | tr 'A-Z' 'a-z')
LAN_IP=$(j .lan_ip)
LAN_CIDR=$(j .lan_cidr)
LAN_PREFIX=${LAN_CIDR#*/}
ONPREM_CIDR=$(j .onprem_cidr)
ONPREM_ASN=$(j .onprem_asn)
AWS_ASN=$(j .aws_asn)
MTU=$(j .mtu)
MSS=$(j .mss)
N_TUNNELS=$(jq '.tunnels | length' "$CFG")

WAN_IF=$(ip -o route show default | awk '{print $5; exit}')

# --- LAN interface: attached after boot, so find it by MAC and wait ---
LAN_IF=""
for _ in $(seq 1 60); do
  LAN_IF=$(ip -o link | awk -F': ' -v mac="$LAN_MAC" 'tolower($0) ~ mac {print $2; exit}')
  [ -n "$LAN_IF" ] && break
  sleep 5
done
[ -n "$LAN_IF" ] || { echo "LAN interface with MAC $LAN_MAC never appeared" >&2; exit 1; }
ip addr replace "$LAN_IP/$LAN_PREFIX" dev "$LAN_IF"
ip link set "$LAN_IF" up

# --- forwarding ---
cat > /etc/sysctl.d/90-netcortex.conf <<SYSCTL
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
SYSCTL
sysctl --system >/dev/null

# --- PSKs from Parameter Store (instance profile creds can lag at first boot) ---
declare -a PSK
for i in $(seq 0 $((N_TUNNELS - 1))); do
  name=$(j ".tunnels[$i].psk_parameter")
  for _ in $(seq 1 30); do
    if PSK[i]=$("$AWS" ssm get-parameter --region "$REGION" --name "$name" \
        --with-decryption --query Parameter.Value --output text 2>/dev/null); then
      break
    fi
    sleep 10
  done
  [ -n "${PSK[i]:-}" ] || { echo "could not read $name" >&2; exit 1; }
done

# --- route-based VTIs (vti1, vti2): strongSwan must not install its own routes ---
cat > /etc/strongswan.d/netcortex.conf <<STRONGSWAN
charon {
  install_routes = no
  install_virtual_ip = no
}
charon-systemd {
  install_routes = no
  install_virtual_ip = no
}
STRONGSWAN

for i in $(seq 0 $((N_TUNNELS - 1))); do
  n=$((i + 1))
  vti="vti$n"
  remote=$(j ".tunnels[$i].outside_ip")
  mark=$(j ".tunnels[$i].mark")
  inside_local=$(j ".tunnels[$i].inside_local")
  ip link del "$vti" 2>/dev/null || true
  ip tunnel add "$vti" local "$LOCAL_IP" remote "$remote" mode vti key "$mark"
  ip addr add "$inside_local/30" dev "$vti"
  ip link set "$vti" up mtu "$MTU"
  sysctl -qw "net.ipv4.conf.$vti.disable_policy=1" "net.ipv4.conf.$vti.rp_filter=0"
done

# --- strongSwan (swanctl): IKEv2, AES256/SHA2-256/DH20 (ecp384) on both phases ---
umask 077
{
  echo "connections {"
  for i in $(seq 0 $((N_TUNNELS - 1))); do
    n=$((i + 1))
    remote=$(j ".tunnels[$i].outside_ip")
    mark=$(j ".tunnels[$i].mark")
    cat <<CONN
  aws-t$n {
    version = 2
    local_addrs = $LOCAL_IP
    remote_addrs = $remote
    proposals = aes256-sha256-ecp384
    dpd_delay = 10s
    mobike = no
    local {
      auth = psk
      id = $PUBLIC_IP
    }
    remote {
      auth = psk
      id = $remote
    }
    children {
      aws-t$n {
        local_ts = 0.0.0.0/0
        remote_ts = 0.0.0.0/0
        esp_proposals = aes256-sha256-ecp384
        mark_in = $mark
        mark_out = $mark
        start_action = start
        dpd_action = restart
        close_action = start
      }
    }
  }
CONN
  done
  echo "}"
  echo "secrets {"
  for i in $(seq 0 $((N_TUNNELS - 1))); do
    n=$((i + 1))
    remote=$(j ".tunnels[$i].outside_ip")
    printf '  ike-aws-t%s {\n    id = %s\n    secret = "%s"\n  }\n' "$n" "$remote" "${PSK[i]}"
  done
  echo "}"
} > /etc/swanctl/conf.d/aws.conf
umask 022
unset PSK

systemctl restart strongswan.service 2>/dev/null || systemctl restart strongswan-swanctl.service
swanctl --load-all >/dev/null

# --- iptables: MSS clamp on the tunnels, MASQUERADE for LAN internet egress ---
# The clamp lives in its own chain so the "MSS clamp removed" fault is one flush.
iptables -t mangle -N NETCORTEX-MSS 2>/dev/null || true
iptables -t mangle -F NETCORTEX-MSS
iptables -t mangle -A NETCORTEX-MSS -o vti+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss "$MSS"
iptables -t mangle -A NETCORTEX-MSS -i vti+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss "$MSS"
iptables -t mangle -C FORWARD -j NETCORTEX-MSS 2>/dev/null || iptables -t mangle -A FORWARD -j NETCORTEX-MSS
iptables -t nat -C POSTROUTING -s "$LAN_CIDR" -o "$WAN_IF" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s "$LAN_CIDR" -o "$WAN_IF" -j MASQUERADE

# --- FRR bgpd: two eBGP neighbors over the VTIs, advertise the on-prem summary ---
sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons
{
  echo "frr defaults traditional"
  echo "hostname onprem-router"
  echo "log syslog informational"
  echo "!"
  echo "ip route $ONPREM_CIDR blackhole"
  echo "!"
  echo "ip prefix-list ONPREM seq 5 permit $ONPREM_CIDR"
  echo "!"
  for i in $(seq 0 $((N_TUNNELS - 1))); do
    n=$((i + 1))
    prepend=$(j ".tunnels[$i].as_path_prepend")
    lp=$(j ".tunnels[$i].local_pref")
    echo "route-map AWS-OUT-T$n permit 10"
    echo " match ip address prefix-list ONPREM"
    if [ "$prepend" -gt 0 ]; then
      path=""
      for _ in $(seq 1 "$prepend"); do path="$path $ONPREM_ASN"; done
      echo " set as-path prepend$path"
    fi
    echo "!"
    echo "route-map AWS-IN-T$n permit 10"
    echo " set local-preference $lp"
    echo "!"
  done
  echo "router bgp $ONPREM_ASN"
  echo " bgp router-id $LOCAL_IP"
  echo " no bgp ebgp-requires-policy"
  for i in $(seq 0 $((N_TUNNELS - 1))); do
    peer=$(j ".tunnels[$i].inside_remote")
    echo " neighbor $peer remote-as $AWS_ASN"
    echo " neighbor $peer timers 10 30"
  done
  echo " !"
  echo " address-family ipv4 unicast"
  echo "  network $ONPREM_CIDR"
  for i in $(seq 0 $((N_TUNNELS - 1))); do
    n=$((i + 1))
    peer=$(j ".tunnels[$i].inside_remote")
    echo "  neighbor $peer route-map AWS-IN-T$n in"
    echo "  neighbor $peer route-map AWS-OUT-T$n out"
  done
  echo " exit-address-family"
  echo "!"
} > /etc/frr/frr.conf
chown frr:frr /etc/frr/frr.conf
chmod 640 /etc/frr/frr.conf
systemctl restart frr

echo "netcortex router up: wan=$WAN_IF lan=$LAN_IF tunnels=$N_TUNNELS"
