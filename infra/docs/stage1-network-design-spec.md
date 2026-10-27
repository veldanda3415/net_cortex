NetCortex AWS Lab — Stage 0a + Stage 1 Network Design Spec (Oct 24 2026)
 
Scope: core plus all cheap add-ons. Region us-east-1. Infra code lives in `net_cortex/infra/`.
Containerlab is deferred to next week. PR A/B (provider connectivity, detection catalog) run in parallel on the code side. Nothing here depends on them until Phase 3.
 
---
 
## 1. Decisions in this spec (review these first)
 
| # | Decision | Why |
|---|---|---|
| D1 | **The on-prem VPC is NOT attached to the TGW.** It reaches AWS only through the Site-to-Site VPN. | This corrects the handoff. If on-prem had a TGW attachment, traffic would bypass the VPN and every VPN/BGP fault would be invisible. |
| D2 | **Centralized egress is core, not an add-on.** A security VPC holds a NAT GW, and app/shared send 0/0 to it via the TGW. | Private instances need outbound access for SSM, packages, and the CloudWatch agent. The alternative is 3 interface endpoints × 2 VPCs (~$0.06/hr), which costs more than one NAT and teaches less. |
| D3 | **Egress and inspection share one security VPC.** Inspection is toggled with `enable_inspection`, which flips TGW route tables. | This is the standard AWS centralized-inspection pattern, and it saves an attachment. Turning inspection on and off is itself a realistic, CloudTrail-visible change. |
| D4 | **Inspection firewalls run in 2 AZs.** Appliance mode is on for the security attachment. | Appliance mode only matters when the TGW can pick different AZs for each direction of a flow. With a single AZ, the "appliance mode off → asymmetric drops" fault cannot happen. |
| D5 | **Access is SSM Session Manager only.** No SSH, no bastion, no inbound 22. | This is how real companies run it. It also keeps CloudTrail as the audit record of operator actions. |
| D6 | **Terraform state is split by lifecycle, not by component.** Three roots: `bootstrap` (persistent), `lab` (ephemeral, feature flags), and later `eks` (ephemeral). | Splitting by component makes up/down slow and fragile. Splitting by lifecycle and blast radius is the defensible principle. |
| D7 | **Instances use Graviton (t4g) with Ubuntu 24.04 arm64.** | Cheapest, and FRR plus strongSwan are packaged for arm64. |
| D8 | **AZs are pinned by AZ ID (use1-az1, use1-az2) and resolved to names with a data source.** | AZ names map differently per account; AZ IDs don't. This is a common interview probe. |
| D9 | **The coding agent never runs `terraform apply` or `destroy`.** Ravi reads every plan and applies. | Keeps the reviewer gate the handoff requires. |
 
**Fault catalog corrections (verified against AWS docs Oct 24):**
- **BGP MD5 mismatch: likely impossible on S2S VPN.** No BGP authentication option appears in the documented tunnel options. The agent must confirm against the `ModifyVpnTunnelOptions` API; if absent, drop this fault.
- **Hold-timer mismatch is not a fault.** BGP negotiates the lower hold time, so the session still comes up. Replace it with DPD timeout/action changes and phase-2 lifetime mismatch.
- **New fault: prefix-limit.** A TGW VPN accepts up to 1,000 dynamic routes from the customer gateway (a VGW accepts 100). AWS doesn't document what happens past the limit. Advertising 1,001+ prefixes from FRR is a genuine experiment, and a good interview story.
- **MTU/MSS fault is grounded.** AWS requires a 1399-byte tunnel MTU and doesn't support PMTUD on VPN. Removing the MSS clamp produces real black-holing of large packets.
---
 
## 2. Stage 0a — AWS bootstrap (persistent, ~$0/month)
 
**Manual prerequisites (Ravi, console, one time):**
- Root MFA on, no root access keys.
- Enable IAM Identity Center and create an admin permission set for yourself. Use `aws sso login` so you never hold long-lived access keys.
- Account-level settings:
  - EBS default encryption on.
  - IMDSv2 required by default (`modify-instance-metadata-defaults`).
  - Cost Anomaly Detection: one monitor plus email (free).
**Terraform root `infra/bootstrap/`.** This root keeps local state, then migrates itself into the bucket it creates.
- **S3 state bucket** `netcortex-tfstate-<account_id>`:
  - versioning on, SSE-S3, all public access blocked
  - a deny-non-TLS policy
  - noncurrent versions expire after 30 days
  - SSE-S3 instead of KMS saves the $1/month CMK. Know that answer: state contains secrets such as VPN PSKs.
- **Locking:** S3 native locking (`use_lockfile = true`, Terraform ≥ 1.10). No DynamoDB table; DynamoDB locking is deprecated.
- **AWS Budget:** $50 monthly, with email alerts at 50%, 80% and 100% actual and at 100% forecast.
- **GitHub OIDC provider + role `netcortex-gha-plan`:**
  - Trust is limited to `repo:veldanda3415/net_cortex` pull requests and `main`.
  - Permissions: read-only infrastructure plus state bucket read/write.
  - The apply role comes in Stage 1 PR 1, together with the nightly reaper.
---
 
## 3. Topology
 
```
                         ┌──────────── Security VPC 10.30/16 ────────────┐
                         │ tgw-subnets(a,b) → fw EC2 (a,b) → NAT(a) → IGW │
                         └───────────────┬───────────────────────────────┘
                                         │ attachment (appliance mode)
 App VPC 10.10/16 ──attach──┐      ┌─────┴──────┐      ┌──attach── Shared VPC 10.20/16
 NLB(internal) → app EC2 a,b└──────┤    TGW     ├──────┘          svc EC2 (EKS in Stage 2)
 iperf3 server                     │ ASN 64512  │
   │ VPC peering                   └─────┬──────┘
 Partner VPC 10.40/16                    │ VPN attachment, 2 tunnels, BGP
 (non-transitive target)                 │ (over the internet path to AWS VPN endpoints)
                               ┌─────────┴─────────┐
                               │ On-prem VPC 172.16/16 (NOT on TGW)
                               │ router EC2: strongSwan VTIs + FRR ASN 65010, EIP
                               │ lan: client + traffic generator
                               └───────────────────┘
```
 
### CIDR and subnet plan
| VPC | CIDR | Subnets |
|---|---|---|
| app | 10.10.0.0/16 | private-a 10.10.0.0/20, private-b 10.10.16.0/20, tgw-a 10.10.255.0/28, tgw-b 10.10.255.16/28 |
| shared | 10.20.0.0/16 | private-a 10.20.0.0/19, private-b 10.20.32.0/19 (room for EKS VPC-CNI pod IPs), tgw-a/b /28 at 10.20.255.x |
| security | 10.30.0.0/16 | public-a 10.30.0.0/24 (NAT), fw-a 10.30.10.0/24, fw-b 10.30.11.0/24, tgw-a/b /28 at 10.30.255.x |
| partner | 10.40.0.0/16 | private-a 10.40.0.0/24 |
| on-prem | 172.16.0.0/16 | wan 172.16.0.0/24 (router eth0 + EIP), lan 172.16.10.0/24 (router eth1, client) |
 
All cloud VPCs fall under 10.0.0.0/8, which keeps the summary routes simple. On-prem uses 172.16/16, so it looks like a different corporate network.
 
### TGW
- ASN 64512.
- Default route-table association and propagation **disabled**, so every route table is explicit.
- `vpn_ecmp_support = enable` and DNS support on.
- **Route tables:**
  - `rt-spoke` (associated with app and shared):
    - Inspection off: 0/0 → security, plus propagations from app, shared and VPN.
    - Inspection on: only 0/0 → security.
  - `rt-onprem` (associated with VPN):
    - Inspection off: propagations from app and shared.
    - Inspection on: static 10.0.0.0/8 → security.
  - `rt-security` (associated with security): propagations from app, shared and VPN. These are the return paths.
- Interview point: one boolean flipping propagation vs static routes is exactly how inspection insertion works in production.
### Security VPC routing
| Route table | Inspection on | Inspection off |
|---|---|---|
| tgw-subnet (per AZ) | 0/0 → fw ENI in same AZ | 0/0 → NAT |
| fw-subnet | 0/0 → NAT, 10/8 and 172.16/16 → TGW | — |
| public | 0/0 → IGW, 10/8 and 172.16/16 → TGW | same |
 
- Firewall instances:
  - source/dest check off, `ip_forward=1`
  - iptables FORWARD with conntrack: allow established, allow the lab flows, log and drop everything else
  - a simple, explicit ruleset; this is the stateful device whose behaviour the RCA must explain
### VPN and BGP
- A TGW VPN attachment with a customer gateway pointing to the router's EIP, using BGP ASN 65010.
- **Tunnel parameters:**
  - inside CIDRs 169.254.100.0/30 and 169.254.100.4/30 (agent verifies these are outside AWS-reserved ranges)
  - IKEv2, AES256/SHA2-256/DH20 on both phases
  - DPD timeout 30s with action restart
  - startup action start
  - tunnel activity logs **and BGP logs** to CloudWatch Logs (BGP logging launched Nov 2025)
- **PSKs:** generated with `random_password`, stored in SSM Parameter Store SecureString (standard tier, free). The router fetches them at boot.
  - Never put PSKs in user_data, which is readable through DescribeInstanceAttribute.
  - The PSK is still in Terraform state; that's accepted, because the state bucket is encrypted and access-restricted.
- **Router (t4g.small):**
  - strongSwan (swanctl) with route-based VTIs: vti1 and vti2, MTU 1399, MSS clamp 1359.
  - FRR bgpd with two neighbors (169.254.100.1 and .5), advertising 172.16.0.0/16.
  - iptables MASQUERADE for the lan subnet's internet traffic, so on-prem hosts can reach SSM.
- **`vpn_mode` flag, all in one stack:**
  - `bgp_active_standby` (default): AS-path prepend on tunnel 2 outbound, local-pref inbound.
  - `bgp_ecmp`: both tunnels equal.
  - `static`: recreates the attachment, ~5–10 min, so use it only for dedicated sessions.
### Workloads (all tiny, rate-capped)
- **App VPC:**
  - 2 × t4g.nano running nginx with `/health`, behind an internal NLB spanning 2 AZs.
  - An iperf3 server on app-a.
- **Shared VPC:** 1 × t4g.nano "svc". It curls `app.lab.internal` every 5s; this is the east-west, DNS-dependent flow.
- **On-prem lan client:**
  - curls the NLB every 2s
  - iperf3 to app-a capped at **2 Mbps**
  - pings the partner (expected to fail; this is the non-transitivity check)
- **Partner VPC:** 1 × t4g.nano nginx, peered with app only. App-a curls it every 10s.
- **DNS:**
  - Private hosted zone `lab.internal` associated with app, shared and security, containing `app` (alias to the NLB) and `svc` records.
  - Route 53 Resolver query logging for app and shared, to CloudWatch Logs.
  - On-prem doesn't resolve the PHZ, because a Resolver inbound endpoint costs ~$0.25/hr. Accepted.
---
 
## 4. Telemetry each component must emit
 
This is the input to the catalog (PR B) and to the AWS source plugins (Phase 3).
 
| Component | Signals | Source |
|---|---|---|
| VPN | TunnelState per tunnel, TunnelDataIn/Out | CloudWatch `AWS/VPN` |
| VPN | IKE/IPsec negotiation, BGP state changes and errors | VPN tunnel activity logs + BGP logs → CloudWatch Logs |
| TGW | PacketDropCountBlackhole, PacketDropCountNoRoute, Bytes/Packets per attachment | CloudWatch `AWS/TransitGateway` |
| TGW | Per-flow drops incl. no-route / blackhole / MTU-exceeded fields (agent verifies field names) | TGW Flow Logs → CloudWatch Logs |
| TGW | Route installed/uninstalled, VPN IPsec/BGP up/down events | Network Manager events. **Gotcha:** delivered to **us-west-2**, so add a us-west-2 EventBridge rule forwarding to the us-east-1 bus. Needs TGW registration in a global network. |
| TGW | Current route tables | `SearchTransitGatewayRoutes` API (state, polled at incident time) |
| VPCs | ACCEPT/REJECT flows: custom format with tcp-flags, flow-direction, traffic-path, pkt-src/dst, 1-min aggregation | VPC Flow Logs → CloudWatch Logs, 3-day retention |
| NAT | ErrorPortAllocation, PacketsDropCount, ActiveConnectionCount | CloudWatch `AWS/NATGateway` |
| NLB | UnHealthyHostCount, HealthyHostCount, TCP_Target_Reset_Count | CloudWatch `AWS/NetworkELB` |
| DNS | NXDOMAIN/SERVFAIL by name and VPC | Resolver query logs → CloudWatch Logs |
| Router | bgp_peer_state per neighbor, prefixes_received, prefixes_sent (60s) | cron: `vtysh ... json` → PutMetricData. 6 custom metrics, ~$1/mo prorated. |
| Router | FRR and charon logs | CloudWatch agent → CloudWatch Logs |
| Firewalls | iptables drop logs, conntrack count | CloudWatch agent |
| Control plane | Who changed SG/NACL/route/TGW/VPN options, and when | CloudTrail event history (free, 90 days). Note LookupEvents is throttled at ~2 TPS per region, which the plugin must respect. |
| Traffic | HTTP success/latency per flow, iperf throughput/loss | traffic scripts → CloudWatch custom metrics (namespace `NetCortexLab/Traffic`) |
 
**Not used, and why:** EC2 detailed monitoring (paid, and 5-minute basic metrics are enough for host context), CloudTrail trails to S3 (event history suffices), Reachability Analyzer (charged per analysis; a possible Phase 5 "validation" tool).
 
---
 
## 5. Fault catalog v2
 
| Fault | Injection | Primary evidence NetCortex should find |
|---|---|---|
| BGP neighbor shutdown | `vtysh: neighbor X shutdown` | BGP log Idle, router bgp_peer_state, Network Manager BGP-DOWN, TGW route uninstalled |
| One tunnel down (partial) | `swanctl --terminate` tunnel 1 | TunnelState 0 on one tunnel only; traffic continues (an "is it an outage?" test) |
| ASN mismatch | wrong `remote-as` | BGP log OPEN error, session never Established |
| Bad route-map | deny 172.16/16 outbound | session up but 0 prefixes, TGW route uninstalled, flows REJECT/no-route |
| Prefix-limit | advertise 1,001+ statics | unknown AWS behavior: the experiment |
| IKE proposal mismatch | `modify-vpn-tunnel-options` to restrict AWS side | CloudTrail change + IPsec DOWN in tunnel logs |
| DPD/lifetime mismatch | strongSwan timers | tunnel flaps, IKE rekey errors |
| MSS clamp removed | delete iptables clamp | small requests OK, large ones hang; TGW MTU-exceeded drops |
| TGW blackhole | static blackhole route for 10.10.0.0/20 | PacketDropCountBlackhole, CloudTrail CreateTransitGatewayRoute |
| TGW propagation removed | disable propagation | route gone, NoRoute drops, CloudTrail |
| SG rule removed | revoke app ingress from 172.16/16 | Flow Log REJECTs, NLB health OK (asymmetric symptom), CloudTrail |
| NACL deny | deny on app private subnet | REJECTs both directions, CloudTrail |
| Appliance mode off | modify the security attachment | intermittent drops in firewall conntrack logs (asymmetric) |
| Firewall instance stopped in one AZ | stop fw-a | flows via AZ-a blackholed, one AZ healthy |
| NAT route removed | delete 0/0 → NAT | egress fails, SSM agents go offline |
| Peering route removed | delete app → partner route | partner flow fails; on-prem unaffected |
| PHZ record deleted / VPC disassociated | Route 53 change | NXDOMAIN spike in query logs, svc curl fails, CloudTrail |
| NLB health-check blocked | SG blocks the health-check port | UnHealthyHostCount, client errors |
| Loss/latency | `tc netem` on router vti | traffic loss/latency, no control-plane change (a "no config culprit" test) |
| App CPU overload | `stress-ng` on app-a | CPU, NLB latency |
 
---
 
## 6. Cost estimate (us-east-1 on-demand, approximate; agent verifies with the AWS Pricing API in PR 1)
 
| Item | $/hr |
|---|---|
| TGW attachments ×4 (app, shared, security, VPN) | 0.20 |
| S2S VPN connection | 0.05 |
| NAT GW | 0.045 |
| Internal NLB | 0.023 |
| Public IPv4 ×2 (router EIP, NAT) | 0.01 |
| EC2: 1 × t4g.small + 7 × t4g.nano | 0.046 |
| EBS 8 × 8 GB gp3 | 0.007 |
| **Fixed subtotal** | **~0.38** |
| Variable: TGW $0.02/GB, NAT $0.045/GB, CloudWatch Logs ingest $0.50/GB (capped traffic) | ~0.05 |
| **Lab total** | **~$0.45/hr** → ~$1.80 per 4-hr session |
 
Twelve sessions come to about $22, which leaves room for EKS in Stage 2 (~$0.10/hr control plane plus nodes).
 
**The biggest risk is forgetting to destroy:** about $10/day. A nightly reaper is mandatory before the first apply (PR 1).
 
---
 
## 7. Terraform layout and build order
 
```
infra/
  bootstrap/            persistent: state bucket, budget, OIDC, roles
  modules/              vpc, tgw, security_vpc, onprem_router, vpn, workload, dns, peering, flowlogs
  live/lab/             one ephemeral root; feature flags:
                        enable_inspection, enable_peering, enable_dns, enable_nlb, vpn_mode
  Makefile              make plan / make up / make down / make status
.github/workflows/      terraform-plan on PR (OIDC plan role); nightly-reaper (02:00 ET)
```
 
- Default tags on everything: `Project=netcortex-lab`, `Ephemeral=true`, `Owner=ravi`, `ManagedBy=terraform`.
- Pins: AWS provider latest 6.x, Terraform ≥ 1.10.
- **Build order, one PR each, stacked or merged in sequence:**
  1. **PR 1 — Network core:** app, shared and security VPCs, TGW and its route tables, NAT egress, VPC Flow Logs, plus the reaper workflow and its apply role. Exit: an instance in app reaches the internet via TGW → NAT, and Flow Logs arrive.
  2. **PR 2 — On-prem and VPN:** on-prem VPC, router, CGW, VPN attachment, BGP, PSKs in Parameter Store, VPN tunnel and BGP logs. Exit: both tunnels UP, BGP Established, 172.16/16 present in rt-spoke.
  3. **PR 3 — Workloads and traffic:** app instances, NLB, svc, client, traffic scripts with custom metrics, DNS zone and query logs. Exit: all flows green in a CloudWatch dashboard.
  4. **PR 4 — Telemetry plumbing:** TGW Flow Logs, router metrics cron and CloudWatch agent, Network Manager registration with the us-west-2 → us-east-1 event forwarding, and baseline CloudWatch alarms. Exit: every row in §4 produces data.
  5. **PR 5 — Add-ons:** the inspection toggle with firewalls, peering and partner, and the `vpn_mode` variants. Exit: each toggle applies and reverts cleanly.
  6. **PR 6 — Fault scripts:** `infra/faults/<name>/{inject,revert}.sh`, using SSM Run Command plus the AWS CLI. Each script is idempotent and logs to a `faults` log group.
  The fault log group is ground truth for evals; NetCortex must never read it.
---
 
## 8. Coding-agent prompts
 
### Standing rules (all prompts)
- Create the named branch from latest `main`; do all work on it.
- Commit as the repo's configured git user only. Do not add any AI/Claude author, `Co-Authored-By` trailer, or "generated with" line to commits or PR descriptions.
- Push the branch to GitHub. Never merge into `main`, never push to `main`, never force-push. A draft PR is fine; the owner merges.
- **Infra-specific rules:**
  - Never run `terraform apply` or `destroy`.
  - Run `terraform fmt -check`, `terraform validate` and `tflint`.
  - Run `terraform plan` only if credentials are present, and paste the plan summary into the PR.
  - No credentials, account IDs or emails hard-coded; use variables and data sources.
### Prompt — Stage 0a: bootstrap
Branch: `infra/bootstrap`.
Implement §2 of `stage1-network-design-spec.md` (put a copy in `infra/docs/`) as `infra/bootstrap/`:
- S3 state bucket with the listed controls.
- S3 native locking, documented for downstream roots.
- AWS Budget with the four notifications to `var.alert_email`.
- GitHub OIDC provider and the `netcortex-gha-plan` role, trust-scoped to this repo's pull requests and `main`, with read-only infrastructure plus state-bucket permissions.
- Local state first, then a documented `terraform init -migrate-state` step into the new bucket.
- A `README.md` listing the manual prerequisites from §2 as a checklist.
- Outputs: bucket name, plan role ARN.
### Prompt — Stage 1 PR 1: network core
Branch: `infra/network-core`.
Implement §3 (app, shared and security VPCs; TGW; route tables with the `enable_inspection` flag wired but firewalls not yet built, so inspection must fail validation if enabled; NAT egress), §4's VPC Flow Logs row, and §7's layout, Makefile and default tags.
- Resolve AZs from AZ IDs.
- Add the nightly reaper GitHub workflow and a `netcortex-gha-apply` role:
  - trusted only for scheduled runs and manual dispatch on `main`
  - runs `terraform destroy` on `infra/live/lab` when state has resources
  - reports to the job summary
- Verify the §6 prices with the AWS Pricing API or the docs; put a cost table in the PR description, flagging any line that differs by more than 20%.
- Exit test: document an SSM session on a temporary test instance in app that curls the internet, and a Logs Insights query showing its flows.
