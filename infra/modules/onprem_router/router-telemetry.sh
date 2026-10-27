#!/bin/bash
# NetCortex on-prem router telemetry. Runs at every boot (netcortex-router-telemetry.service),
# separately from bring-up so a telemetry failure never blocks routing.
# - rsyslog splits FRR and charon messages into their own files
# - the CloudWatch agent ships those files to CloudWatch Logs
# - a timer publishes per-neighbor BGP state and prefix counts every minute
set -euo pipefail

CFG=/etc/netcortex/router.json
j() { jq -r "$1" "$CFG"; }

REGION=$(j .region)

# --- rsyslog: FRR daemons log via syslog, charon-systemd via the journal ---
cat > /etc/rsyslog.d/30-netcortex.conf <<'RSYSLOG'
if $programname == ['bgpd', 'zebra', 'staticd', 'mgmtd', 'watchfrr'] then /var/log/netcortex-frr.log
if $programname startswith 'charon' then /var/log/netcortex-charon.log
RSYSLOG
systemctl restart rsyslog

# --- CloudWatch agent (regional download; the WAN has direct internet access) ---
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
jq -n --arg region "$REGION" --arg frr "$(j .log_groups.frr)" --arg charon "$(j .log_groups.charon)" '{
  agent: { region: $region, run_as_user: "root" },
  logs: { logs_collected: { files: { collect_list: [
    { file_path: "/var/log/netcortex-frr.log", log_group_name: $frr, log_stream_name: "{instance_id}" },
    { file_path: "/var/log/netcortex-charon.log", log_group_name: $charon, log_stream_name: "{instance_id}" }
  ] } } }
}' > "$AGENT_CFG"
"$CTL" -a fetch-config -m ec2 -s -c "file:$AGENT_CFG"

# --- BGP metrics every minute ---
systemctl enable --now netcortex-bgp-metrics.timer

echo "netcortex router telemetry up"
