# Adopting NetCortex in Your Network

This page is the shortest path from "cloned the repo" to "NetCortex explains my incidents". Each step works on its own and adds one capability. You can stop after any of them.

| Step | You provide | You get |
|---|---|---|
| 1. Try it offline | nothing | the full pipeline on recorded lab incidents |
| 2. Connect telemetry | read access to metrics, logs, changes | per-domain findings for your alarms |
| 3. Send alerts | a webhook or queue from your alerting | an RCA per alarm burst, anchored on the alarm time |
| 4. Add topology | a list of what depends on what | agents ask each other about upstream causes; causes ranked by position |
| 5. Teach it your names | JSON entries for your signals | detection of your own error classes and metrics |
| 6. Record and replay | a directory | every live incident becomes an offline test |

## 1. Try it offline

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python app/main.py eval --cases evals/cases          # 10 real-lab faults, replayed
python app/main.py run --scenario 1 --no-require-llm # a simulated incident
```

Reports land in `output/<incident>/rca_report.json`. Read the following fields:
- `root_cause`
- `ranked_causes`: why each candidate scored where it did
- `a2a_message_log`: which agent asked which, about what, and the answer
- `incident_status`

## 2. Connect telemetry

NetCortex reads telemetry only through a telemetry MCP server (`python -m mcp_telemetry_server --config <file>`). Credentials live there, never in the RCA process. Enable the source plugins you have:

| You run | Plugin | Notes |
|---|---|---|
| AWS | `cloudwatch`, `cloudwatch_logs`, `cloudtrail`, `tgw_routes` | `config/telemetry.aws-lab.yaml` is a complete example; another account changes names, not code |
| Prometheus, Mimir, Cortex | `prometheus` | one PromQL expression per signal; `tenant` for Mimir |
| Azure, GCP, ELK, Splunk, on-prem | write a plugin | one class; see [provider-adapter-guide.md](provider-adapter-guide.md) |

The most useful minimum is **metrics plus change records** (CloudTrail, Activity Log, Git or Ansible change logs): most outages are a change. Point the RCA side at the server under `telemetry.domains` in your config (`config/config.aws-lab.yaml` shows how).

## 3. Send alerts

`POST /incidents` with a description, a region, and, importantly, `reported_at` (when the alarm fired) and `alarm_entities` (what it fired on):

```json
{"description": "flow svc-to-app below 100% success", "region": "us-east-1", "severity": "high",
 "reported_at": "2026-10-28T15:02:00Z", "alarm_entities": [{"type": "flow", "id": "svc-to-app"}]}
```

`ingestion/cloudwatch_alarms.py` maps CloudWatch alarm events, including their dimensions to `alarm_entities`. `ingestion/sqs_alarm_bridge.py` pulls them from a queue and groups a burst into one incident, so NetCortex needs no public endpoint. An Alertmanager or Azure Monitor adapter is the same shape.

## 4. Add topology

This is what turns parallel agents into collaborating ones. A topology source answers `get_topology` with entities and relationships. The relationship types are in `contract/catalog/entities.json`, each with which end depends on the other.
- **On AWS**, enable `aws_topology`. It reads VPN, TGW, VPC, security groups, load balancers and Route 53 with describe calls. List your synthetic probes under `flows`.
- **Elsewhere**, a topology plugin can be as simple as reading an inventory export (NetBox, a CMDB, a YAML file).

**One rule matters: use the same ids your metrics and logs use.** Put other names in labels. A tunnel whose metrics say `3.226.61.62` can have topology id `vpn-…-tunnel-1` with label `outside_ip: 3.226.61.62`, and NetCortex matches either.

## 5. Teach it your names

What a signal means is JSON in `contract/catalog/`, overridable per environment through `$NETCORTEX_CATALOG_DIR`:
- **`signals.json`:** a signal and its rule (threshold, state, z-score, rate change, event presence).
- **`investigation.json`:**
  - `signal_roles`: whether a signal usually means cause or symptom
  - `probes`: which domains can be asked about an entity type
  - `role_with`: conditional roles, for example "PrefixesSent=0 is a symptom while BGP is down"

Validate before deploying:

```bash
python app/main.py catalog validate ./my-catalog
```

## 6. Record and replay

Set `telemetry.record_dir` and every incident's telemetry and topology are saved as a replay fixture. Re-run any of them offline, with or without the LLM, after changing rules or code:

```bash
GEMINI_API_KEY= python -m evals.replay_recordings output/alarm_bridge/<incident>.json
```

Turn the ones that matter into `evals/cases/*.json` entries, and `eval --cases` becomes your regression suite.

## What to expect

- **What NetCortex is good at:** it finds a change or a state flip near the alarm, explains how it reaches the alarmed entity, and says when nothing is wrong. Confidence is computed; the LLM only reorders near-ties and writes the sentence, which must cite rule-fired evidence.
- **What it can't do:** it only knows what the plugins fetch. A fault with no telemetry of its own (on our lab, a TCP MSS clamp the cloud rewrites anyway) is invisible by design. Unclear cases come back as "symptom" or "inconclusive", not as a guess.
