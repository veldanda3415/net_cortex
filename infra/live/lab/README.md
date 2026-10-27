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

The flag `enable_inspection` is wired into the TGW route tables, but it's rejected until the firewalls land in PR 5.

Costs about **$0.33/hr** while it's up: 4 TGW attachments, the VPN, 1 NAT, 2 public IPv4 and the t4g.small router. Logs add a little.

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
