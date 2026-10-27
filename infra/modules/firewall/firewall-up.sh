#!/bin/bash
# NetCortex inspection firewall bring-up. Runs at every boot (netcortex-firewall.service).
# A deliberately small, explicit, stateful ruleset (spec §3): allow established
# flows and the lab's own flows, log and drop everything else. This is the
# stateful device whose behaviour the RCA must explain.
set -euo pipefail

CFG=/etc/netcortex/firewall.json
j() { jq -r "$1" "$CFG"; }

REGION=$(j .region)
ONPREM=$(j .cidrs.onprem)
APP=$(j .cidrs.app)
SHARED=$(j .cidrs.shared)
CLOUD=$(j .cidrs.cloud)
IF=$(ip -o route show default | awk '{print $5; exit}')

cat > /etc/sysctl.d/90-netcortex.conf <<SYSCTL
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.$IF.send_redirects = 0
SYSCTL
sysctl --system >/dev/null

# --- filter: FORWARD defaults to drop; new flows must match the lab allow-list ---
iptables -P FORWARD DROP
for chain in NETCORTEX-FW NETCORTEX-EGRESS; do
  iptables -N "$chain" 2>/dev/null || true
  iptables -F "$chain"
done
iptables -F FORWARD
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -m conntrack --ctstate INVALID -j LOG --log-prefix "NETCORTEX-FW-INVALID " -m limit --limit 10/s
iptables -A FORWARD -m conntrack --ctstate INVALID -j DROP
iptables -A FORWARD -m conntrack --ctstate NEW -j NETCORTEX-FW
iptables -A FORWARD -j LOG --log-prefix "NETCORTEX-FW-DROP " -m limit --limit 10/s
iptables -A FORWARD -j DROP

# Lab flows (east-west).
iptables -A NETCORTEX-FW -s "$ONPREM" -d "$APP" -p tcp --dport 80 -j ACCEPT
iptables -A NETCORTEX-FW -s "$ONPREM" -d "$APP" -p tcp --dport 5201 -j ACCEPT
iptables -A NETCORTEX-FW -s "$ONPREM" -d "$APP" -p udp --dport 5201 -j ACCEPT
iptables -A NETCORTEX-FW -s "$SHARED" -d "$APP" -p tcp --dport 80 -j ACCEPT
iptables -A NETCORTEX-FW -p icmp --icmp-type echo-request -j ACCEPT
# Internet egress for lab hosts (packages, SSM, metrics, NTP).
iptables -A NETCORTEX-FW -s "$CLOUD" -j NETCORTEX-EGRESS
iptables -A NETCORTEX-FW -s "$ONPREM" -j NETCORTEX-EGRESS
iptables -A NETCORTEX-EGRESS -d "$CLOUD" -j RETURN
iptables -A NETCORTEX-EGRESS -d "$ONPREM" -j RETURN
iptables -A NETCORTEX-EGRESS -p tcp -m multiport --dports 80,443 -j ACCEPT
iptables -A NETCORTEX-EGRESS -p udp --dport 123 -j ACCEPT

# --- nat: internet-bound flows leave with this firewall's address, so replies
# from the single NAT come back through the same (stateful) firewall ---
iptables -t nat -N NETCORTEX-SNAT 2>/dev/null || true
iptables -t nat -F NETCORTEX-SNAT
iptables -t nat -C POSTROUTING -j NETCORTEX-SNAT 2>/dev/null || iptables -t nat -A POSTROUTING -j NETCORTEX-SNAT
iptables -t nat -A NETCORTEX-SNAT -d "$CLOUD" -j RETURN
iptables -t nat -A NETCORTEX-SNAT -d "$ONPREM" -j RETURN
iptables -t nat -A NETCORTEX-SNAT -o "$IF" -j MASQUERADE

# --- telemetry: drop logs to their own file, shipped by the CloudWatch agent ---
cat > /etc/rsyslog.d/30-netcortex.conf <<'RSYSLOG'
:msg, contains, "NETCORTEX-FW-" /var/log/netcortex-fw-drops.log
& stop
RSYSLOG
systemctl restart rsyslog

CTL=/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl
if [ ! -x "$CTL" ]; then
  DEB=/tmp/amazon-cloudwatch-agent.deb
  URL="https://amazoncloudwatch-agent-$REGION.s3.$REGION.amazonaws.com/ubuntu/arm64/latest/amazon-cloudwatch-agent.deb"
  for attempt in $(seq 1 30); do
    curl -fsSL -o "$DEB" "$URL" && break
    [ "$attempt" -lt 30 ] || { echo "could not download the CloudWatch agent" >&2; exit 1; }
    sleep 10
  done
  dpkg -i -E "$DEB"
  rm -f "$DEB"
fi

AGENT_CFG=/opt/aws/amazon-cloudwatch-agent/etc/netcortex.json
jq -n --arg region "$REGION" --arg drops "$(j .log_group)" '{
  agent: { region: $region, run_as_user: "root" },
  logs: { logs_collected: { files: { collect_list: [
    { file_path: "/var/log/netcortex-fw-drops.log", log_group_name: $drops, log_stream_name: "{instance_id}" }
  ] } } }
}' > "$AGENT_CFG"
"$CTL" -a fetch-config -m ec2 -s -c "file:$AGENT_CFG"

systemctl enable --now netcortex-fw-metrics.timer

echo "netcortex firewall up: if=$IF"
