# NetCortex

NetCortex is a simulation-first, multi-agent root cause analysis (RCA) system for network incidents.
It orchestrates domain agents (metrics, logs, routing, config), performs A2A-style cross-agent collaboration,
and synthesizes both machine-structured and human-readable RCA output.

## Highlights

- Multi-agent RCA workflow built on LangGraph.
- FastAPI-based domain agents exposing ADK-compatible A2A agent cards and task messaging.
- Deterministic confidence scoring plus Gemini LLM-based incident classification and narrative synthesis.
- Simulation scenarios for reproducible local development and demos.
- Replay/eval mode to run bundled scenarios and score expected RCA keyword coverage.
- Console progress logging so users can track execution stages in real time.

## Documentation

For contributors and production-oriented extensions, use these guides:

- [docs/agent-authoring-guide.md](docs/agent-authoring-guide.md): how domain agents decide, score confidence, and collaborate.
- [docs/decision-policy.md](docs/decision-policy.md): how NetCortex turns multi-agent evidence into one RCA conclusion.
- [docs/provider-adapter-guide.md](docs/provider-adapter-guide.md): how to connect Prometheus, ELK, Splunk, MCP, or custom backends.

Recommended reading order for contributors:

1. `docs/agent-authoring-guide.md`
2. `docs/decision-policy.md`
3. `docs/provider-adapter-guide.md`

## Repository Layout

- `app/main.py`: CLI entrypoint for one-shot run and server mode.
- `core/orchestrator.py`: workflow graph and stage orchestration.
- `agents/`: domain agents and RCA synthesizer.
- `communication/`: registry and A2A router.
- `ingestion/`: webhook API intake and normalization.
- `providers/simulation/`: scenario-backed provider implementations.
- `providers/adapters/`: stubs for real-system integrations.
- `config/config.yaml`: runtime behavior and timeouts.
- `scripts/send_incident.py`: helper for webhook injection.
- `tests/`: unit, integration, and e2e placeholders.

## Quickstart

## 5-Minute Quick Run

If you just want to prove the project works end-to-end:

1. Install dependencies.
2. Run one incident.
3. Confirm output artifacts were created.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app/main.py run --scenario 1
```

If you do not have LLM auth configured yet, run in deterministic fallback mode:

```powershell
python app/main.py run --scenario 1 --no-require-llm
```

## Detailed Setup

### 1) Create and activate virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2) Configure environment

Edit `config/.env`.

Supported LLM auth modes:

- API key mode:
  - set `GEMINI_API_KEY=...`
- ADC + Vertex AI mode:
  - set `GOOGLE_CLOUD_PROJECT=...`
  - optional `GOOGLE_CLOUD_LOCATION=us-central1`
  - authenticate with `gcloud auth application-default login`

If no LLM auth is available and strict LLM mode is disabled, NetCortex falls back to deterministic synthesis.

Important:

- LLM strict mode is enabled by default.
- Without valid LLM auth, one-shot runs fail fast.
- To run in deterministic fallback mode for local experimentation, use the CLI switch `--no-require-llm`.

### 3) Run a one-shot incident

```powershell
python app/main.py run --scenario 1
```

### 4) Run tests

```powershell
python -m pytest -q
```

## Runtime Console Traction and Logging

NetCortex now emits stage-by-stage progress logs during execution.

Examples of log events:

- Runtime startup and agent registration.
- Supervisor classification and selected active agents.
- Per-agent analysis result, timeout, or error.
- Collaboration rounds and convergence.
- Synthesizer start/finish and final confidence.

Run with default info logs:

```powershell
python app/main.py run --scenario 1
```

Run with debug logs:

```powershell
python app/main.py run --scenario 1 --verbose
```

Replay/eval all bundled scenarios (useful for reproducibility demos and CI):

```powershell
python app/main.py eval --all-scenarios --fail-on-miss
```

Evaluate a single scenario:

```powershell
python app/main.py eval --scenario 10
```

LLM strict mode is optional. Enable it explicitly when you want fail-fast behavior if LLM is unavailable:

```powershell
python app/main.py run --scenario 1 --require-llm
```

Default mode is deterministic fallback (no strict LLM requirement). The following is equivalent and explicit:

```powershell
python app/main.py run --scenario 1 --no-require-llm
```

Print full JSON report to console (optional):

```powershell
python app/main.py run --scenario 1 --print-json
```

By default, NetCortex prints a readable RCA summary and file location, not the full JSON blob.

## Expected Output

On a successful run (`python app/main.py run --scenario 1`), you should see:

- Agent registration lines with skills, for example:
  `Registered agent=metrics endpoint=http://localhost:8001/a2a skills=analyze-metrics,respond-to-peer`
- Supervisor classification line, for example:
  `Supervisor classified incident=<id> degradation_type=network active_agents=metrics,log,routing,config`
- Analysis result lines from each active agent.
- Final console report block titled `NetCortex RCA Report`.

A run creates artifacts under `output/<incident_id>/`:

- `rca_report.json`
- `agent_trace.jsonl`
- `a2a_messages.jsonl`
- `supervisor_state.json`

Detailed logs are written to `output/log/`.

Detailed log files:

- `runtime.log`
- `orchestrator.log`
- `synthesizer.log`
- `metrics.log`
- `log.log`
- `routing.log`
- `config.log`

## Webhook Mode and External Incident Injection

Start service mode:

```powershell
python app/main.py serve
```

Send scenario incident from helper:

```powershell
python scripts/send_incident.py send --scenario 3
```

Send custom incident JSON:

```powershell
python scripts/send_incident.py send --file sample_incident.json
```

PowerShell direct POST:

```powershell
$body = @{
  description = "Packet loss spike and throughput drop in us-east after maintenance window"
  region = "us-east"
  severity = "high"
  scenario_id = 1
  source_system = "external-monitor"
  external_incident_id = "INC-1001"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8000/incidents -ContentType "application/json" -Body $body
```

Request fields:

- `description`: required string.
- `region`: required string.
- `severity`: one of `low`, `medium`, `high`, `critical`.
- `scenario_id`: optional integer.
- `source_system`: optional string.
- `external_incident_id`: optional string.

## Reading RCA Output

Reports are written to:

- `output/<incident_id>/rca_report.json`

Recommended reading order:

1. `root_cause`
2. `human_readable_summary`
3. `contributing_factors`
4. `causal_chain`
5. `agent_findings[].key_events`
6. `a2a_message_log`

Key fields:

- `root_cause`: concise primary cause statement.
- `human_readable_summary`: plain-English explanation (LLM when available).
- `confidence_score`: synthesized confidence score.
- `corroborating_domain_count`: number of anomaly-supporting domains.
- `conflict_detected`: whether signals conflict.

Conflict-focused demo scenarios:

- Scenario `10` demonstrates a `metrics` anomaly on Switch-C with no corroborating config change (`config` reports no changes).
- Scenario `11` demonstrates mixed evidence (optic/log anomalies with limited cross-domain corroboration).
- Scenario `12` demonstrates explicit cross-domain conflict (`log` + `routing` anomalies with mostly clean `metrics`) to exercise `conflict_detected` behavior.

## Configuration Notes

Runtime behavior is controlled from `config/config.yaml`.

Important settings:

- `llm.model`: Gemini model name.
- `a2a.protocol_mode`: `adk` (SDK-native A2A client) or `custom` (fallback transport).
- `a2a.analysis_timeout_seconds`: per-agent analysis timeout.
- `a2a.max_iterations`: collaboration rounds cap.
- `simulation.region` and `simulation.window_minutes`.

Schema note:
The current A2A message schema (see `models/schemas.py`, `A2AMessage`) is a baseline contract used by this implementation. It is intentionally extensible and can be expanded with additional message types and payload fields as collaboration needs evolve.

Validation checks enforce compatible timeout relationships at startup.

## Integrating Real Data Sources

Simulation providers are enabled by default.

Adapter extension points:

- `providers/adapters/prometheus_adapter.py`
- `providers/adapters/elk_adapter.py`
- `providers/adapters/splunk_adapter.py`
- `providers/adapters/mcp_adapter.py`

To move toward production-like integrations, implement adapter methods and update provider mode in `config/config.yaml`.

## Open Source Metadata

- License: MIT (see `LICENSE`).
- Secrets: `config/.env` is ignored by git; never commit credentials.
- Development artifacts and runtime outputs are ignored in `.gitignore`.

## Contribution Workflow

1. Fork and create a feature branch.
2. Run tests locally: `python -m pytest -q`.
3. Keep changes scoped and documented.
4. Open a pull request with a clear summary, test evidence, and any config impacts.

## Troubleshooting

- If LLM output is missing, verify API key or ADC project settings.
- If you see GCP quota project warnings, align ADC quota project with intended billing project.
- If Typer help errors appear, ensure dependencies are installed from requirements.txt (currently click==8.3.1 and typer==0.16.1).

## Verification Checklist

1. Create and activate `.venv`.
2. Install dependencies.
3. Run `python -m pytest -q`.
4. Run `python app/main.py --help`.
5. Run `python app/main.py run --scenario 1`.
6. Confirm logs show workflow progress.
7. Inspect output report artifacts.
