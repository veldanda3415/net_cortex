# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

NetCortex is a simulation-first, multi-agent root cause analysis (RCA) system for network incidents. It orchestrates domain agents (metrics, logs, routing, config) over an A2A-style protocol, runs collaboration rounds where agents revise findings based on peer evidence, and synthesizes a single RCA report (deterministic aggregation + optional Gemini LLM narrative).

The repo is mid-migration from a pure simulation/A2A system to also exposing/consuming MCP (2026-07-28 spec, Streamable HTTP): `mcp_server/` exposes NetCortex RCA itself as MCP tools, and `mcp_telemetry_server/` + `providers/adapters/mcp_adapter.py` let domain agents fetch telemetry over MCP instead of simulation data. `Impl_plan.md` is the source of truth for migration scope/sequencing — check it before touching MCP-related files.

## Commands

```bash
python -m venv .venv && .\.venv\Scripts\activate   # Windows; repo is developed on Windows (PowerShell examples in README)
python -m pip install -r requirements.txt

# One-shot incident run (starts the 4 domain agent servers in-process, runs one incident, exits)
python app/main.py run --scenario 1
python app/main.py run --scenario 1 --no-require-llm   # deterministic fallback, no LLM auth needed
python app/main.py run --scenario 1 --verbose --print-json

# Long-running service mode: domain agents + webhook ingestion on :8000
python app/main.py serve

# NetCortex-as-MCP-server (2026-07-28 spec, Streamable HTTP)
python app/main.py mcp-serve --port 9000

# Replay/eval bundled scenarios, scoring expected RCA keyword coverage
python app/main.py eval --all-scenarios --fail-on-miss
python app/main.py eval --scenario 10

# Tests
python -m pytest -q
python -m pytest tests/unit/test_short_circuit.py -q       # single file
python -m pytest tests/unit/test_short_circuit.py::test_short_circuit_true_when_all_no_anomaly -q  # single test

# MCP smoke test (adapter/server wiring)
python scripts/mcp_smoke_test.py

# K8s deploy (local cluster: minikube/k3s/Docker Desktop)
python deploy.py            # build + apply
python deploy.py --status
python deploy.py --teardown
```

LLM strict mode (`llm.require_success` / `--require-llm`) makes classification and synthesis fail fast without valid `GEMINI_API_KEY` or ADC (`GOOGLE_CLOUD_PROJECT`); default is deterministic fallback. Config comes from `config/config.yaml` plus `config/.env` (gitignored, loaded via `load_env_file`).

## Architecture

**Pipeline (LangGraph, `core/orchestrator.py`):** `supervisor -> analysis -> collaboration -> synthesizer -> END`, driven by `NetCortexEngine.run_incident`.

- **supervisor**: `agents/supervisor.py` classifies `degradation_type` (LLM with heuristic fallback) and selects the minimal-but-safe agent set via `select_active_agents` (`_AGENT_SETS`). Network/unknown degradation always runs all 4 domains because incident descriptions state symptoms, not causes — don't "optimize" this pruning without re-reading the design note in that file.
- **analysis**: fans out `send_analysis` to each active agent concurrently via the configured router, with a per-agent timeout (`a2a.analysis_timeout_seconds`); timeouts/errors land in `timed_out_agents`, not a hard failure.
- **collaboration**: broadcasts each finding to peers (`router.broadcast`), then calls `apply_local_reconsideration` — this applies each agent's `reconsider_finding(finding, peer_findings)` **locally in the orchestrator**, not via agents processing live A2A messages. That's a known Phase-0 simplification (see the `NOTE` in `orchestrator.py`); don't assume peer reconsideration happens inside the agent HTTP handlers even though those handlers also have their own (separate, session-based) peer-message queuing/reconsideration path for direct A2A calls. Rounds repeat until findings converge or `a2a.max_iterations` is hit. `should_short_circuit` skips collaboration entirely when no agent found an anomaly.
- **synthesizer**: `agents/rca_synthesizer.py` deterministically aggregates confidence/conflict signals, with an optional Gemini call for `human_readable_summary` and richer causal-chain text; on LLM failure it always has a deterministic fallback.

**Contract**: every domain agent must emit `AgentFinding` (`models/schemas.py`) — this is the shared currency between agents, orchestrator, and synthesizer. Agent Cards (served at `/.well-known/agent.json`) advertise `analyze-<domain>` and `respond-to-peer` skills plus `schemaContract.outputSchema=AgentFinding`; `AgentRegistry` (`communication/agent_registry.py`) fails registration if these are missing. See `docs/agent-authoring-guide.md` for the full 6-step per-agent decision pipeline and `docs/decision-policy.md` for how findings become one RCA conclusion.

**Transport is pluggable**: `config/config.yaml`'s `a2a.protocol_mode` selects `communication/a2a_router.py` (`A2ARouter`, custom JSON-RPC-ish transport over `httpx`) or `communication/adk_a2a_router.py` (`ADKA2ARouter`, wraps the `google-adk`/`a2a` SDK client, with fallback code paths for old vs. new SDK message/part shapes). Both implement the same `send_analysis` / `send_direct` / `broadcast` surface (`communication/router_base.py`), so the orchestrator is transport-agnostic.

**Data is pluggable per-domain**: `providers/factory.py` builds a `MetricsProvider`/`LogProvider`/`RoutingProvider`/`ConfigProvider` per `config.providers.<domain>` (`simulation` or `mcp`), keyed off `providers/base.py` abstract interfaces. Simulation implementations live in `providers/simulation/` and read from `simulation/scenarios.py` (`SCENARIOS`, keyed by `scenario_id`). MCP-backed implementations live in `providers/adapters/mcp_adapter.py` and hit `mcp_endpoints.<domain>.url` (an `mcp_telemetry_server` instance). Real-system adapters (`prometheus_adapter.py`, `elk_adapter.py`, `splunk_adapter.py`) are extension-point stubs — see `docs/provider-adapter-guide.md`. Anomaly baselines are a separate concept (`BaselineProvider` in `providers/base.py`, `providers/baseline_utils.py` for z-score math, `baselines.provider` = `simulation` or `prometheus`) used by the metrics and config agents; when no baseline exists for an entity, `baselines.legacy_fallback` controls whether a hard-coded threshold is used instead.

**Config validation**: `core/orchestrator.py:validate_config` enforces timeout ordering (`message_timeout_seconds < round_timeout_seconds`, `round_timeout_seconds * max_iterations <= collaboration_timeout_seconds`, `analysis_timeout_seconds < collaboration_timeout_seconds`) and baseline settings, and raises `ValueError("ConfigValidationError: ...")` — this runs at `NetCortexEngine` construction, so a bad `config.yaml` fails fast before any incident runs.

**Ingestion**: `ingestion/webhook_server.py` (`WebhookServer`) exposes `POST /incidents` for `serve` mode; `scripts/send_incident.py` is the CLI helper for posting scenario or custom-JSON incidents to it.

**MCP layer**: `mcp_server/server.py` exposes `run_rca` (async, task-based — see `mcp_server/tasks.py` for `TaskStore`/`TaskStatus`), `get_report`, `list_scenarios` as MCP tools, wrapping the same `NetCortexEngine`. `mcp_telemetry_server/server.py` exposes `get_metrics`/`get_logs`/`get_routing_events`/`get_config_changes` MCP tools backed by `SCENARIOS`, standing in for real Prometheus/ELK/Splunk-backed MCP servers.

**Outputs**: each run writes `output/<incident_id>/{rca_report.json, agent_trace.jsonl, a2a_messages.jsonl, supervisor_state.json}` and per-component logs to `output/log/*.log` (`app/main.py:configure_runtime_logging`); `output/` is gitignored.

## Working in this repo (migration-specific)

These rules come from `.github/copilot-instructions.md` and `.github/instructions/mcp-migration.instructions.md` (both gitignored, local-only steering files already present in this checkout):

- For MCP migration work, prefer changes in `mcp_server/`, `mcp_telemetry_server/`, `providers/adapters/mcp_adapter.py`, `providers/factory.py`, `config/config.yaml`, `app/main.py`, `k8s/`, `README.md`.
- Keep `core/orchestrator.py`, `models/schemas.py`, `providers/base.py`, `simulation/scenarios.py`, and `agents/*.py` stable unless the task explicitly requires a contract change — fix migration issues at the adapter/server/wiring layer first.
- Keep RCA and telemetry MCP services stateless, and keep simulation mode as the safe default path — provider factory changes must not break local eval runs or change eval behavior unless that's the explicit task.
- Assume local Kubernetes, single replica per service, deferred OAuth, in-cluster HTTP service-to-service comms (see `k8s/*.yaml`), unless newer repo docs override this.
- Before closing out MCP migration work, run `python app/main.py eval --all-scenarios --fail-on-miss` when the environment permits.

<!-- BEGIN AWS Agent Toolkit rules -->
# AWS Guidance

- Where these AWS rules conflict with the project's own instructions, the
  project's instructions take precedence.
- Prefer the AWS MCP Server for AWS interactions — it provides sandboxed
  execution, observability, and audit logging. If unavailable, use the
  AWS CLI directly.
- Before starting a task, check whether a relevant AWS skill is available.
  Load the skill with `retrieve_skill` and prefer its guidance over
  general knowledge.
- When uncertain about specific AWS details (API parameters, permissions,
  limits, error codes), verify against documentation rather than guessing.
  State uncertainty explicitly if you cannot confirm.
- When creating infrastructure, prefer infrastructure-as-code (AWS CDK or
  CloudFormation) over direct CLI commands.
- When working with infrastructure, follow AWS Well-Architected Framework
  principles.
- Do not use em dashes in AWS resource names or descriptions. Use
  hyphens instead.

## Secret Safety

- MUST load the `aws-secrets-manager` skill first for any secret,
  credential, API key, token, or password task. MUST NOT call
  `secretsmanager get-secret-value` or `batch-get-secret-value`, and MUST
  NOT hit the Secrets Manager Agent daemon directly. MUST use
  `{{resolve:secretsmanager:secret-id:SecretString:json-key}}` with
  `asm-exec` so the secret resolves at runtime without entering context.
<!-- END AWS Agent Toolkit rules -->
