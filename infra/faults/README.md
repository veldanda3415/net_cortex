# infra/faults

Fault scripts for the lab (spec §5, §7 PR 6). Each fault is a folder with `inject.sh` and `revert.sh`. They use SSM Run Command for anything on a host, and the AWS CLI for everything else.

- **Idempotent.** Every script checks the current state first. Running inject twice, or revert on a healthy lab, does nothing and logs `already`.
- **Logged.** Every run writes one JSON event (`fault`, `action`, `result`, `detail`, `at`, `by`) to `/netcortex-lab/faults`, one stream per fault.
- **Ground truth.** That log group is the answer key for evals. **NetCortex must never read it.**
- **Toggle-aware.** A fault that doesn't apply to the current toggles exits 2 and logs `skipped`. For example, `firewall-stopped` without inspection.

Run them from `infra/` with the lab up. They use `AWS_PROFILE`, `aws-veldanda-admin` by default.

```bash
faults/tgw-blackhole/inject.sh
faults/tgw-blackhole/revert.sh

# Inject, time the lab alarms, revert, time the recovery; prints one JSON line
faults/drill.sh tgw-blackhole
```

## Catalog

| Fault | What it does | Needs |
|---|---|---|
| `bgp-neighbor-shutdown` | Router shuts its BGP session to tunnel 1's peer | BGP |
| `tunnel-down` | Router drops all traffic to tunnel 1's endpoint, so only tunnel 2 is left | |
| `asn-mismatch` | Router expects AS 64999 on tunnel 1 (`WRONG_ASN`) | BGP |
| `bad-route-map` | Router stops advertising 172.16/16 on both tunnels; sessions stay up | BGP |
| `prefix-limit` | Router advertises 1,001 extra /24s (`COUNT`), past the TGW's 1,000-route limit. AWS drops BGP on both tunnels: a full on-prem outage | BGP |
| `ike-proposal-mismatch` | AWS side of tunnel 1 accepts only DH group 21; the router offers 20. Takes a few minutes each way. | |
| `mss-clamp-removed` | Router's TCP MSS clamp flushed. A no-op on AWS: the VPN side rewrites MSS to 1350 anyway (seen in a capture), so it's a negative control | |
| `loss-latency` | `tc netem` on both tunnels, 150ms and 5% loss (`DELAY`, `LOSS`); no config change anywhere | |
| `tgw-blackhole` | Blackhole route in rt-onprem for app private-a (app-a and one NLB node) | |
| `tgw-propagation-removed` | App stops propagating into rt-onprem | inspection off |
| `sg-rule-removed` | App web SG's HTTP-from-on-prem rule narrowed to TEST-NET (same rule ID, so no Terraform drift after revert); health checks stay green | |
| `nlb-healthcheck-blocked` | App web SG's HTTP-from-the-app-VPC rule (the NLB's health checks) narrowed to TEST-NET. Both targets go unhealthy, the NLB fails open, and clients keep working | |
| `nacl-deny` | App NACL rule 50 denies on-prem both ways | |
| `nat-route-removed` | Security VPC loses 0/0 → NAT. Spoke egress, SSM and the traffic metrics go dark, so no alarm fires | inspection off |
| `peering-route-removed` | App loses its route to the partner VPC | peering |
| `phz-record-deleted` | `app.lab.internal` deleted; svc gets NXDOMAIN | |
| `appliance-mode-off` | Appliance mode off on the security attachment | inspection |
| `firewall-stopped` | fw-a stopped | inspection |
| `app-cpu-overload` | `stress-ng` on every vCPU of app-a; stops itself after 30 min (`DURATION`) | |
| `subnet-route-table-swapped` | App private-a moved from its route table to the VPC's main table (local route only). Uncovered: `DisassociateRouteTable`/`AssociateRouteTable` aren't in the catalog | |
| `nat-gateway-deleted` | The security VPC's NAT gateway deleted. Like `nat-route-removed`, but the cause (`DeleteNatGateway`) is uncovered and the gateway leaves the topology | inspection off |
| `frr-max-prefix` | Router accepts at most 1 prefix (`MAX_PREFIX`) from tunnel 2's peer; FRR logs `%MAXPFX` and a Cease 6/1, lines no classify rule knows | BGP |
| `mtu-blackhole` | Router silently drops packets of 1200+ bytes (`LEN`) on the tunnels, no ICMP. No config change and no log line: "no cause verified" is the right answer | |

**Uncovered faults** (the last four) have no catalog rule for their cause. Only the LLM triage can name them, and only once the agents verify its hypothesis with code (see `detection/hypotheses.py`). `subnet-route-table-swapped` and `nat-gateway-deleted` can't be reverted under the same IDs (an association or a NAT gateway comes back with a new one), so their revert runs `terraform apply` (`tf_reconcile` in `lib.sh`); state and lab agree afterwards, and `make plan` shows no changes.

The expected evidence for each fault is in the comment at the top of its `inject.sh`.

**Not included from the spec:**
- BGP MD5 mismatch. AWS S2S VPN has no BGP authentication option.
- Hold-timer mismatch. BGP negotiates the lower value, so the session still comes up.
- DPD/lifetime mismatch. IKEv2 doesn't negotiate lifetimes; each side rekeys on its own timer, so a mismatch doesn't break anything either. `tunnel-down` and `ike-proposal-mismatch` cover tunnel failures.

## Scoring NetCortex

With NetCortex running against the lab, `drill.sh` also scores its RCA. Every lab alarm that goes into ALARM reaches an SQS queue through EventBridge. The alarm bridge turns each burst of alarms into one `POST /incidents`. The drill then holds the fault until the RCA arrives, and scores it against this fault's events in the faults log, using `expect.json` and `score.py`.

```bash
# once: a profile for the read-only NetCortex role (it is denied the faults log group)
aws configure set profile.netcortex-lab.role_arn "$(terraform -chdir=live/lab output -raw netcortex_role_arn)"
aws configure set profile.netcortex-lab.source_profile aws-veldanda-admin
aws configure set profile.netcortex-lab.region us-east-1

# three terminals, from the repo root
python -m mcp_telemetry_server --config config/telemetry.aws-lab.yaml --port 9001
python app/main.py serve --config config/config.aws-lab.yaml
python app/main.py alarm-bridge --profile netcortex-lab \
  --queue-url "$(terraform -chdir=infra/live/lab output -raw alarm_queue_url)"

# then, from infra/
faults/drill.sh tgw-blackhole     # the output line gains a `netcortex` key
```

- **Verdicts.** `netcortex.verdict` is one of these:
  - `cause`: the RCA's evidence includes a signal that points at the culprit.
  - `symptom`: it saw only the impact.
  - `miss`: it saw neither.
  - `clean` or `false_positive`: for the negative control, `mss-clamp-removed`.
  - `stale`: the RCA's window ended before the injection.
- **No alarm.** If no alarm fires (`loss-latency`, `app-cpu-overload`, `mss-clamp-removed`), the drill files a manual incident that says nothing about the fault, so those are scored too.
- **Without NetCortex.** If NetCortex isn't reachable, the drill works as before: alarms only, `netcortex: null`.
- **Pacing.** With NetCortex running, the drill reverts as soon as the RCA report is in, then confirms recovery with `lab_health.py` (no lab alarm in ALARM; flows, BGP peers and tunnels clean in two fresh datapoints) instead of a fixed cooldown.
- **Gaps between drills.** An RCA reads back `window_minutes` plus the question lookback before its alarm (8 + 2 in the lab config). Start the next fault no sooner than that, minus the fastest alarm (2 minutes), after `recovered_at`: 8 minutes in the lab. Otherwise the previous fault's revert calls show up as config changes.

## Notes

- The faults change Terraform-managed resources in place and revert them to the same values and IDs, so a reverted lab plans clean. Revert, or `make down`, before the next `make plan`.
- One fault at a time. Two overlapping faults make the answer key ambiguous.
- `drill.sh` keeps watching for 3 minutes after the revert (`COOLDOWN`), because some metrics, the NLB's especially, land minutes late. An alarm that fires then still belongs to that fault.
- `drill.sh` measures the lab's CloudWatch alarms, which evaluate two of three one-minute periods. That makes about 2.5 minutes the floor for detection from metrics. Logs and CloudTrail evidence can show up sooner or later than that.
