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
| `prefix-limit` | Router advertises 1,001 extra /24s (`COUNT`), past the TGW's 1,000-route limit. An experiment. | BGP |
| `ike-proposal-mismatch` | AWS side of tunnel 1 accepts only DH group 21; the router offers 20. Takes a few minutes each way. | |
| `mss-clamp-removed` | Router's TCP MSS clamp flushed; large responses hang | |
| `loss-latency` | `tc netem` on both tunnels, 150ms and 5% loss (`DELAY`, `LOSS`); no config change anywhere | |
| `tgw-blackhole` | Blackhole route in rt-onprem for app private-a (app-a and one NLB node) | |
| `tgw-propagation-removed` | App stops propagating into rt-onprem | inspection off |
| `sg-rule-removed` | App web SG loses HTTP from on-prem; health checks stay green | |
| `nlb-healthcheck-blocked` | App web SG loses HTTP from the app VPC (the NLB's health checks) | |
| `nacl-deny` | App NACL rule 50 denies on-prem both ways | |
| `nat-route-removed` | Security VPC loses 0/0 → NAT; spoke egress, SSM and metrics go dark | inspection off |
| `peering-route-removed` | App loses its route to the partner VPC | peering |
| `phz-record-deleted` | `app.lab.internal` deleted; svc gets NXDOMAIN | |
| `appliance-mode-off` | Appliance mode off on the security attachment | inspection |
| `firewall-stopped` | fw-a stopped | inspection |
| `app-cpu-overload` | `stress-ng` on every vCPU of app-a; stops itself after 30 min (`DURATION`) | |

The expected evidence for each fault is in the comment at the top of its `inject.sh`.

**Not included from the spec:**
- BGP MD5 mismatch. AWS S2S VPN has no BGP authentication option.
- Hold-timer mismatch. BGP negotiates the lower value, so the session still comes up.
- DPD/lifetime mismatch. IKEv2 doesn't negotiate lifetimes; each side rekeys on its own timer, so a mismatch doesn't break anything either. `tunnel-down` and `ike-proposal-mismatch` cover tunnel failures.

## Notes

- The faults change Terraform-managed resources. Always revert, or `make down`, before the next `make plan`, or the plan will show the drift.
- One fault at a time. Two overlapping faults make the answer key ambiguous.
- `drill.sh` measures the lab's CloudWatch alarms, which evaluate two of three one-minute periods. That is the floor for detection from metrics. Logs and CloudTrail evidence can show up sooner or later than that.
