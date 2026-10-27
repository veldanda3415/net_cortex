# infra/live/lab

The ephemeral lab root. Bring it up for a session and tear it down afterwards. The nightly reaper destroys anything left at 07:00 UTC.

**Stage 1 PR 1 (network core)** adds:
- the app, shared and security VPCs
- the TGW with the `rt-spoke`, `rt-onprem` and `rt-security` route tables
- centralized egress (spokes → TGW → security VPC → NAT → internet)
- VPC Flow Logs, kept 3 days

**Stage 1 PR 2 (on-prem and VPN)** adds:
- an "on-prem" VPC, `172.16.0.0/16`, which is **not** on the TGW
- a router instance running strongSwan VTIs and FRR BGP (ASN 65010)
- a Site-to-Site VPN on the TGW: 2 tunnels, IKEv2, BGP, active/standby on tunnel 1
- PSKs stored in Parameter Store, and tunnel activity and BGP logs in CloudWatch

**Stage 1 PR 3 (workloads and traffic)** adds:
- two nginx servers (`app-a`, `app-b`) behind an internal NLB, plus an iperf3 server on `app-a`
- `svc` in the shared VPC and `client` on the on-prem LAN, which generate steady traffic
- the private zone `lab.internal` (`app` → NLB) and Resolver query logs for app and shared
- one CloudWatch dashboard, `netcortex-lab`, showing every flow, the NLB targets and the tunnels

| Flow | From → to | What it shows |
|---|---|---|
| `onprem-to-nlb` | client → NLB, every 2s | the main on-prem path over the VPN |
| `onprem-to-nlb-large` | client → NLB `/large.bin` (256 KiB), every 10s | MTU/MSS problems (small requests OK, large hang) |
| `onprem-iperf-app-a` | client → app-a, UDP 2 Mbps for 10s each minute | loss and jitter |
| `svc-to-app` | svc → `app.lab.internal`, every 5s | east-west traffic that depends on DNS |

Each flow publishes `Requests`, `Failures`, `SuccessRate` and latency or throughput to `NetCortexLab/Traffic` (dimension `Flow`) once a minute. Failures are also logged with their reason: `journalctl -u netcortex-traffic`.

**Stage 1 PR 4 (telemetry)** adds the rest of the telemetry from spec §4:
- TGW Flow Logs (`/netcortex-lab/tgw-flow/core`), which record why the TGW dropped a flow: no route, blackhole, MTU or TTL
- router BGP metrics every minute (`NetCortexLab/Router`: `BgpPeerState`, `PrefixesReceived`, `PrefixesSent` per neighbor)
- router FRR and strongSwan (charon) logs, shipped by the CloudWatch agent (`/netcortex-lab/router/{frr,charon}`)
- Network Manager: the TGW registered in a global network. Its events (VPN/BGP up and down, routes installed and removed) arrive in us-west-2, get forwarded to us-east-1 and are written to `/netcortex-lab/networkmanager/events`
- 13 baseline alarms with no actions, shown on the dashboard. All OK means the lab is healthy.

The firewall logs (iptables drops, conntrack) come with the inspection firewalls below.

**Stage 1 PR 5 (add-ons)** adds three toggles:

| Flag | Default | What it does |
|---|---|---|
| `enable_peering` | `true` | Partner VPC `10.40.0.0/16`, peered with app only. app-a calls it every 10s (`app-to-partner`). The client checks every 30s that it *cannot* reach it (`onprem-to-partner`, healthy = blocked), because peering is not transitive. |
| `enable_inspection` | `false` | Two Linux firewalls (fw-a, fw-b) in the security VPC. All spoke, on-prem and egress traffic goes through the firewall in the same AZ. They allow established flows and the lab's own flows, and log and drop everything else to `/netcortex-lab/firewall/drops`. Metrics: `NetCortexLab/Firewall` `ConntrackCount` and `ForwardDrops`. |
| `vpn_mode` | `bgp_active_standby` | `bgp_ecmp` makes both tunnels equal. `static` removes BGP (static routes on both sides) and **recreates the VPN** (~5-10 min, new tunnel IPs), so only use it in a dedicated session. |

The firewalls also source-NAT internet-bound traffic. With one NAT and a firewall in each AZ, that's what keeps the return path on the same stateful firewall. Without it, the firewall would see only half of each connection.

Costs about **$0.39/hr** while it's up: 4 TGW attachments, the VPN, 1 NAT, the NLB, 2 public IPv4, the t4g.small router and 5 t4g.nano workloads (including the partner). Inspection adds 2 more t4g.nano (~$0.01/hr). Logs and custom metrics add a little. The private zone is free if deleted within 12 hours, so always `make down` the same day.

**Stage 1 PR 6 (faults)** adds the `/netcortex-lab/faults` log group (eval ground truth that NetCortex must never read) and a `fault_targets` output. Together they drive the fault scripts in [`infra/faults/`](../../faults/README.md).

## One-time setup (before the first apply)

1. Apply `infra/bootstrap` again to create the reaper's `netcortex-gha-apply` role.
2. In GitHub, create the environment for the reaper: Settings → Environments → **New environment** `lab-reaper` → Deployment branches → **Selected branches** → add `main`.
3. Add a repository variable: `AWS_APPLY_ROLE_ARN` = the `apply_role_arn` output of `infra/bootstrap`.
4. Run Actions → **nightly-reaper** → Run workflow on `main`. It should finish with "Nothing to reap".

## Session

Run these from `infra/`:

```bash
make plan        # review it
make up          # applies the reviewed plan
make status      # what's running
make down        # destroy at the end of the session
```

## Exit test: egress via TGW → NAT

```bash
make plan TF_ARGS="-var enable_test_instance=true" && make up
ID=$(terraform -chdir=live/lab output -raw test_instance_id)

# Wait about 3 minutes for SSM to register, then:
aws ssm start-session --target "$ID"          # needs the Session Manager plugin
$ curl -s https://checkip.amazonaws.com       # should print the nat_public_ip output
```

Then look at the flows in CloudWatch → Logs Insights, log group `/netcortex-lab/vpc-flow/app`:

```
parse @message "* * * * * * * * * * * * * * * * * * * * * * * * * * *" as version, account, eni, src, dst, srcport, dstport, proto, packets, bytes, start, end, action, status, vpc, subnet, instance, tcpflags, type, pktsrc, pktdst, region, azid, pktsrcsvc, pktdstsvc, direction, path
| filter dstport = 443 and direction = "egress"
| stats count() as flows, sum(bytes) as total_bytes by pktdst, action, path
| sort flows desc
```

You should see `ACCEPT` flows from the test instance to public IPs on 443. Remove the instance afterwards with `make plan && make up` (it's off by default), or just `make down`.

## Exit test: VPN up, BGP established, on-prem route in rt-spoke

The router configures itself at boot, which takes about 5 minutes after `make up` (packages, then tunnels, then BGP).

```bash
VPN=$(terraform -chdir=live/lab output -raw vpn_connection_id)
RTR=$(terraform -chdir=live/lab output -raw router_instance_id)

# 1. Both tunnels UP on the AWS side
aws ec2 describe-vpn-connections --vpn-connection-ids "$VPN" \
  --query 'VpnConnections[0].VgwTelemetry[].[OutsideIpAddress,Status,StatusMessage,AcceptedRouteCount]' --output table

# 2. Both BGP sessions Established on the router; 10.10/16 and 10.20/16 preferred via tunnel 1
aws ssm start-session --target "$RTR"
$ sudo swanctl --list-sas | grep -E 'aws-t|ESTABLISHED|INSTALLED'
$ sudo vtysh -c 'show bgp summary' -c 'show ip route bgp'

# 3. 172.16.0.0/16 propagated from the VPN into rt-spoke
RT=$(terraform -chdir=live/lab output -json tgw_route_table_ids | jq -r .spoke)
aws ec2 search-transit-gateway-routes --transit-gateway-route-table-id "$RT" \
  --filters Name=route-search.exact-match,Values=172.16.0.0/16 --query 'Routes[].[DestinationCidrBlock,Type,State]'
```

If the tunnels stay DOWN, check the router's bring-up log with `journalctl -u netcortex-router` and `/var/log/cloud-init-output.log`, and look at `/netcortex-lab/vpn/tunnel-activity` in CloudWatch.

## Exit test: all flows green

Workloads install their packages at boot; `client` waits for the router, so allow about 10 minutes after `make up`.

```bash
terraform -chdir=live/lab output -raw dashboard_url
```

On the dashboard, every line in **Flow success rate** should sit at 100, **NLB targets** should show 2 healthy and 0 unhealthy, iperf3 should show about 2 Mbps with ~0% loss, and both tunnels should show 1.

From the CLI, the same check for the last 5 minutes:

```bash
for f in onprem-to-nlb onprem-to-nlb-large onprem-iperf-app-a svc-to-app; do
  printf '%-22s ' "$f"
  aws cloudwatch get-metric-statistics --namespace NetCortexLab/Traffic --metric-name SuccessRate \
    --dimensions Name=Flow,Value=$f --statistics Minimum --period 300 \
    --start-time "$(date -u -d '-5 min' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
    --query 'Datapoints[0].Minimum' --output text
done
```

The DNS flow also shows up in the Resolver query logs (`/netcortex-lab/dns/resolver-queries`) as `app.lab.internal` with `NOERROR`.

If a flow stays red, look on that instance with `aws ssm start-session --target <id>` (IDs in the `workload_instance_ids` output), then `journalctl -u netcortex-workload` for bring-up and `journalctl -u netcortex-traffic` for per-request failures.

## Exit test: every telemetry source has data

About 10 minutes after `make up`, each of these should return something:

```bash
since=$(( ($(date +%s) - 900) * 1000 ))
for lg in /netcortex-lab/tgw-flow/core /netcortex-lab/router/frr /netcortex-lab/router/charon \
          /netcortex-lab/networkmanager/events /netcortex-lab/vpn/bgp /netcortex-lab/dns/resolver-queries; do
  printf '%-40s %s events\n' "$lg" \
    "$(aws logs filter-log-events --log-group-name "$lg" --start-time $since --max-items 50 --query 'length(events)')"
done

aws cloudwatch list-metrics --namespace NetCortexLab/Router --query 'Metrics[].[MetricName,Dimensions[0].Value]' --output table
aws cloudwatch describe-alarms --alarm-name-prefix netcortex-lab- --query 'MetricAlarms[].[AlarmName,StateValue]' --output table
```

All alarms should be `OK`. The two VPN alarms and the two BGP alarms start as `ALARM` and clear once the router is up.

The Network Manager log only fills when something changes. Bring-up itself (attachments created, tunnels and BGP coming up) is enough, provided the TGW is registered before the router finishes. If the log is empty, restart the BGP session on the router (`sudo vtysh -c 'clear bgp *'`), and the up/down events should appear within a couple of minutes.

## Exit test: each toggle applies and reverts cleanly

Start from a healthy default lab, with all alarms OK. For each toggle, apply it, wait for the dashboard to go green again, then revert it and check again:

```bash
make plan TF_ARGS="-var enable_inspection=true" && make up   # ~10 min for the firewalls to boot
make plan && make up                                         # back to defaults

make plan TF_ARGS="-var vpn_mode=bgp_ecmp" && make up        # router is replaced, ~5 min for BGP
make plan && make up
```

What to check:
- **Inspection on:** every flow stays at 100%, and both firewalls show conntrack entries. `onprem-to-partner` is now dropped *by the firewall* (look for `NETCORTEX-FW-DROP ... DST=10.40.` in the drop log), not by the router.
- **ECMP:** on the router, `show ip route 10.10.0.0/16` lists two next hops. On the AWS side, both tunnels carry traffic (`TunnelDataIn` on both).
- **Reverted:** the plan back to defaults is clean, and a second `make plan` afterwards shows **no changes**.
