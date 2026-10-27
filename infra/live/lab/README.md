# infra/live/lab

The ephemeral lab root. Bring it up for a session and tear it down afterwards. The nightly reaper destroys anything left at 07:00 UTC.

**Stage 1 PR 1 (network core)** adds:
- the app, shared and security VPCs
- the TGW with the `rt-spoke`, `rt-onprem` and `rt-security` route tables
- centralized egress (spokes → TGW → security VPC → NAT → internet)
- VPC Flow Logs, kept 3 days

The flag `enable_inspection` is wired into the TGW route tables, but it's rejected until the firewalls land in PR 5.

Costs about **$0.21/hr** while it's up: 3 TGW attachments, 1 NAT and 1 public IPv4. Flow logs add a little.

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
