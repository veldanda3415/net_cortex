#!/bin/bash
# NetCortex lab workload bring-up. Runs at every boot (netcortex-workload.service).
# Reads /etc/netcortex/workload.json, installs packages (retrying until the egress
# path is up), then starts nginx, the iperf3 server and the traffic agent as asked.
set -euo pipefail

CFG=/etc/netcortex/workload.json
j() { jq -r "$1" "$CFG"; }

# jq is not on the base image, so read the package list with python3 first.
mapfile -t PACKAGES < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["packages"]))' "$CFG")
PACKAGES+=(jq)

# --- packages: the on-prem client egresses via the router, which may still be booting ---
export DEBIAN_FRONTEND=noninteractive
for attempt in $(seq 1 60); do
  if apt-get update -q && apt-get install -yq --no-install-recommends "${PACKAGES[@]}"; then
    break
  fi
  [ "$attempt" -lt 60 ] || { echo "package install failed after $attempt attempts" >&2; exit 1; }
  sleep 20
done

NAME=$(j .name)

# --- nginx: /health for the NLB, / names the host, /large.bin exercises MTU/MSS ---
if [ "$(j .web)" = "true" ]; then
  mkdir -p /var/www/netcortex
  echo "$NAME" > /var/www/netcortex/index.html
  [ -f /var/www/netcortex/large.bin ] || head -c 262144 /dev/urandom > /var/www/netcortex/large.bin
  cat > /etc/nginx/sites-available/default <<NGINX
server {
  listen 80 default_server;
  root /var/www/netcortex;
  add_header X-Served-By $NAME always;

  location = /health {
    access_log off;
    default_type text/plain;
    return 200 "ok\n";
  }
}
NGINX
  systemctl enable nginx
  systemctl restart nginx
fi

# --- iperf3 server ---
if [ "$(j .iperf_server)" = "true" ]; then
  cat > /etc/systemd/system/netcortex-iperf3.service <<UNIT
[Unit]
Description=iperf3 server for NetCortex lab throughput flows
After=network-online.target

[Service]
ExecStart=/usr/bin/iperf3 -s
Restart=always
DynamicUser=yes

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now netcortex-iperf3.service
fi

# --- memory: these hosts have 512 MB and no swap. fwupd-refresh (firmware updates,
# useless here) needs ~150 MB hourly and got the on-prem client OOM-killed. ---
systemctl mask --now fwupd.service fwupd-refresh.service fwupd-refresh.timer 2>/dev/null || true

# --- CloudWatch agent: memory and swap (CWAgent mem_used_percent, swap_used_percent) ---
# EC2 publishes CPU but not memory; NetCortex judges these against signals.json.
if ! systemctl is-active --quiet amazon-cloudwatch-agent; then
  for attempt in $(seq 1 30); do
    if curl -fsSLo /tmp/amazon-cloudwatch-agent.deb \
        https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/arm64/latest/amazon-cloudwatch-agent.deb; then
      dpkg -i -E /tmp/amazon-cloudwatch-agent.deb && break
    fi
    [ "$attempt" -lt 30 ] || { echo "CloudWatch agent install failed" >&2; break; }
    sleep 20
  done
  mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
  cat > /opt/aws/amazon-cloudwatch-agent/etc/netcortex.json <<'CWAGENT'
{
  "agent": {"metrics_collection_interval": 60},
  "metrics": {
    "append_dimensions": {"InstanceId": "${aws:InstanceId}"},
    "metrics_collected": {
      "mem": {"measurement": ["mem_used_percent"]},
      "swap": {"measurement": ["swap_used_percent"]}
    }
  }
}
CWAGENT
  /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/netcortex.json || echo "CloudWatch agent did not start" >&2
fi

# --- traffic agent ---
if [ "$(j '.flows | length')" -gt 0 ]; then
  systemctl enable --now netcortex-traffic.service
fi

echo "netcortex workload up: $NAME"
