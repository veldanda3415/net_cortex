# NetCortex: Autonomous Agentic RCA for High-Scale Distributed Systems

## 🧠 Overview

NetCortex is an **open-source**, simulation-based agentic system for **automated root cause analysis (RCA)** in distributed systems such as networks, cloud infrastructure, and large-scale services.

It models incident diagnosis as a **collaborative reasoning process between specialized domain agents**, rather than a single centralized analyzer.

The system ships with a fully functional simulation layer so anyone can clone and run it immediately. Production teams can integrate their own data backends (Prometheus, ELK, MCP servers, etc.) via a clean adapter interface — no changes to the core engine required.

Current implementation also exports per-run artifacts (`rca_report.json`, `agent_trace.jsonl`, `a2a_messages.jsonl`, `supervisor_state.json`) so collaborators can inspect exactly what each agent observed and how the final conclusion was formed.

The system is designed to handle:
- Network performance degradation
- Metric anomalies
- Distributed system failures
- Multi-source correlation problems

---

## 🎯 Problem Statement

In large-scale distributed systems, performance degradation signals are scattered across multiple independent subsystems:

- Metrics systems (latency, throughput, error rate, PLR)
- Log systems (failures, timeouts, exceptions)
- Routing systems (path changes, congestion)
- Configuration systems (policy changes, deployments)

### Core Problem:
> There is no single source of truth. Engineers must manually correlate signals across systems to determine root cause, resulting in slow and error-prone diagnosis.

---

## 💡 Key Insight

No single system has full context.

Instead:
- Each subsystem has **partial truth**
- Diagnosis requires **cross-domain reasoning**
- Understanding emerges from **collaboration between agents**

---

## 🚀 Solution

NetCortex introduces a **distributed agentic reasoning system** where:

- Each domain is represented by a specialized agent
- Agents independently analyze their own data
- Agents communicate via the **Google ADK A2A protocol**
- A **LangGraph** supervisor orchestrates the overall workflow state
- A synthesis layer constructs the final RCA report

---

## ⚙️ Technology Stack

| Layer | Technology | Reason |
|---|---|---|
| Language | Python 3.11+ | Ecosystem fit for AI/ML |
| Workflow Orchestration | **LangGraph** | Stateful graph execution, conditional branching, parallel node dispatch |
| Agent Communication Protocol | **Google ADK A2A** | Open standard for inter-agent messaging; HTTP-based, discoverable |
| LLM | Gemini 2.5 Flash (Configurable) | Reasoning for supervisor + synthesizer; swappable via config |
| Data Validation | **Pydantic v2** | Schema enforcement for all messages and outputs |
| Simulation | Custom Python generators | Ships with repo; realistic time-series metric/log/routing data |
| Data Integration | Adapter pattern (abstract base) | Users plug in Prometheus, ELK, MCP, or any backend |
| Config | YAML (`config.yaml`) | User-facing configuration for LLM, providers, timeouts, and MCP endpoints |

> **Dependency note:** Pin `google-adk` to a specific version in `requirements.txt` from day one. The ADK API is still evolving rapidly and unpinned dependencies will break CI unexpectedly. Re-evaluate and bump the pin deliberately with each release.

### LangGraph vs ADK A2A — Role Separation and Integration Seam

These two frameworks serve distinct roles and do not conflict. Understanding exactly how they connect is critical to a correct implementation.

- **LangGraph** owns the *macro workflow* — it manages the state graph, decides which agents are dispatched, handles parallel execution, and drives phase transitions (analysis → A2A collaboration → synthesis). All state merges happen inside LangGraph via reducers.
- **Google ADK A2A** owns the *inter-agent messaging* — it defines how agents discover each other, send structured peer messages, and receive responses. Each domain agent exposes an ADK-compatible A2A endpoint.

#### How They Connect (the Integration Seam)

Each domain agent is modeled as **both** a LangGraph node and an ADK A2A HTTP endpoint:

### Runtime Process Model (Implementation Decision)

NetCortex runs in a **single process** for the initial implementation:
- One Python process started from `main.py`
- FastAPI servers for ingestion + all built-in agent A2A endpoints started as background asyncio tasks
- LangGraph pipeline runs in the same process

Built-in agents expose HTTP endpoints for peer communication, and LangGraph node execution calls those built-in ADK A2A endpoints via self-HTTP to preserve a single protocol path across built-in and external agents.

`a2a_router.py` uses HTTP JSON-RPC only for peer-to-peer communication and external agent endpoints.

At startup, `main.py` must:
1. Start ingestion server and built-in agent endpoint tasks
2. Health-check all `/.well-known/agent.json` endpoints
3. Register agents in `agent_registry.py`
4. Only then accept incidents / run LangGraph workflow

```
LangGraph fan-out
    │
    ├── metrics_agent_node()     ← LangGraph node
    │       │
    │       └── calls ADK A2A endpoint internally → returns AgentFinding to LangGraph state
    │
    ├── log_agent_node()         ← LangGraph node
    │       └── calls ADK A2A endpoint internally → returns AgentFinding to LangGraph state
    │
    ├── routing_agent_node()     ← LangGraph node
    │       └── calls ADK A2A endpoint internally → returns AgentFinding to LangGraph state
    │
    └── config_agent_node()      ← LangGraph node
            └── calls ADK A2A endpoint internally → returns AgentFinding to LangGraph state

Each agent's ADK A2A endpoint ALSO accepts inbound peer messages from other agents
during the A2A collaboration phase.
```

The LangGraph node returns the result into LangGraph state. This preserves LangGraph's reducer and super-step guarantees for the macro workflow, while still enabling lateral A2A peer exchange between agents during the collaboration phase.

**Do not** have ADK A2A agents write results directly to a shared store outside LangGraph — that path loses all state management guarantees.

---

## 📡 ADK A2A Protocol Specification

This section defines the complete ADK A2A protocol implementation for NetCortex. Every agent endpoint, wire format, and lifecycle state is specified here. Implementation must conform exactly to this spec — deviation breaks interoperability with third-party agents.

### Agent Cards

Every domain agent serves an **Agent Card** at `GET /.well-known/agent.json`. This is the ADK A2A discovery contract. The `a2a_router.py` reads Agent Cards at startup to populate the agent registry. Any third-party agent replacing a built-in agent must serve a valid Agent Card at its endpoint for registration to succeed.

#### Metrics Agent Card
```json
{
  "name": "netcortex-metrics-agent",
  "version": "1.0.0",
  "description": "Analyzes metric time-series (latency, error rate, throughput, PLR) to detect anomaly windows relative to rolling baseline.",
  "endpoint": "http://localhost:8001/a2a",
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "analyze-metrics",
      "description": "Fetch and analyze metrics for a region and time window. Returns AgentFinding with anomaly detection result.",
      "inputSchema": {
        "region": "string",
        "window_minutes": "integer",
        "incident_id": "string"
      }
    },
    {
      "id": "respond-to-peer",
      "description": "Respond to clarification or validation requests from peer agents during A2A collaboration.",
      "inputSchema": {
        "message_type": "clarification_request | validation_request",
        "payload": "object",
        "round_number": "integer"
      }
    }
  ],
  "autonomyBoundary": {
    "canDecideAutonomously": ["analyze-metrics", "respond-to-peer"],
    "requiresEscalation": []
  },
  "schemaContract": {
    "outputSchema": "AgentFinding",
    "version": "1.0.0",
    "description": "All responses from this agent conform to the AgentFinding schema. Replacing this agent requires producing the same schema."
  }
}
```

#### Log Agent Card
```json
{
  "name": "netcortex-log-agent",
  "version": "1.0.0",
  "description": "Processes log event streams to detect error clusters, timeout patterns, and correlated anomaly bursts.",
  "endpoint": "http://localhost:8002/a2a",
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "analyze-logs",
      "description": "Fetch and analyze log events for a region and time window. Returns AgentFinding with anomaly detection result.",
      "inputSchema": {
        "region": "string",
        "window_minutes": "integer",
        "incident_id": "string"
      }
    },
    {
      "id": "respond-to-peer",
      "description": "Respond to clarification or validation requests from peer agents during A2A collaboration.",
      "inputSchema": {
        "message_type": "clarification_request | validation_request",
        "payload": "object",
        "round_number": "integer"
      }
    }
  ],
  "autonomyBoundary": {
    "canDecideAutonomously": ["analyze-logs", "respond-to-peer"],
    "requiresEscalation": []
  },
  "schemaContract": {
    "outputSchema": "AgentFinding",
    "version": "1.0.0",
    "description": "All responses from this agent conform to the AgentFinding schema."
  }
}
```

#### Routing Agent Card
```json
{
  "name": "netcortex-routing-agent",
  "version": "1.0.0",
  "description": "Analyzes routing events, path changes, BGP updates, and congestion signals to detect topology-level anomalies.",
  "endpoint": "http://localhost:8003/a2a",
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "analyze-routing",
      "description": "Fetch and analyze routing events for a region and time window. Returns AgentFinding.",
      "inputSchema": {
        "region": "string",
        "window_minutes": "integer",
        "incident_id": "string"
      }
    },
    {
      "id": "respond-to-peer",
      "description": "Respond to clarification or validation requests from peer agents.",
      "inputSchema": {
        "message_type": "clarification_request | validation_request",
        "payload": "object",
        "round_number": "integer"
      }
    }
  ],
  "autonomyBoundary": {
    "canDecideAutonomously": ["analyze-routing", "respond-to-peer"],
    "requiresEscalation": []
  },
  "schemaContract": {
    "outputSchema": "AgentFinding",
    "version": "1.0.0",
    "description": "All responses from this agent conform to the AgentFinding schema."
  }
}
```

#### Config Agent Card
```json
{
  "name": "netcortex-config-agent",
  "version": "1.0.0",
  "description": "Tracks configuration changes, policy updates, and deployments proximate to an incident window.",
  "endpoint": "http://localhost:8004/a2a",
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "analyze-config",
      "description": "Fetch and analyze config change events for a region and time window. Returns AgentFinding.",
      "inputSchema": {
        "region": "string",
        "window_minutes": "integer",
        "incident_id": "string"
      }
    },
    {
      "id": "respond-to-peer",
      "description": "Respond to clarification or validation requests from peer agents.",
      "inputSchema": {
        "message_type": "clarification_request | validation_request",
        "payload": "object",
        "round_number": "integer"
      }
    }
  ],
  "autonomyBoundary": {
    "canDecideAutonomously": ["analyze-config", "respond-to-peer"],
    "requiresEscalation": []
  },
  "schemaContract": {
    "outputSchema": "AgentFinding",
    "version": "1.0.0",
    "description": "All responses from this agent conform to the AgentFinding schema."
  }
}
```

---

### Wire Format — JSON-RPC over HTTP

ADK A2A uses **JSON-RPC 2.0 over HTTP POST**. All agent-to-agent messages in NetCortex use this format. The `a2a_router.py` is responsible for constructing and dispatching these payloads.

#### Task Submission (`tasks/send`)

Used by the supervisor to dispatch analysis tasks to domain agents, and by the A2A collaboration router to send peer messages:

```json
{
  "jsonrpc": "2.0",
  "method": "tasks/send",
  "id": "req-001",
  "params": {
    "id": "task-metrics-INC-20260504-001",
    "sessionId": "INC-20260504-001",
    "message": {
      "parts": [
        {
          "type": "text",
          "text": "Analyze metrics for region us-east, window 30 minutes"
        },
        {
          "type": "data",
          "data": {
            "skill": "analyze-metrics",
            "region": "us-east",
            "window_minutes": 30,
            "incident_id": "INC-20260504-001"
          }
        }
      ]
    }
  }
}
```

#### Peer Message (A2A Collaboration)

During the collaboration loop, peer messages use the same `tasks/send` method. The `skill` field distinguishes peer messages from analysis tasks:

```json
{
  "jsonrpc": "2.0",
  "method": "tasks/send",
  "id": "req-002",
  "params": {
    "id": "task-a2a-metrics-to-routing-round1",
    "sessionId": "INC-20260504-001",
    "message": {
      "parts": [
        {
          "type": "text",
          "text": "Did the path change affect us-east-core specifically?"
        },
        {
          "type": "data",
          "data": {
            "skill": "respond-to-peer",
            "message_type": "clarification_request",
            "sender_agent": "metrics_agent",
            "round_number": 1,
            "payload": {
              "question": "Did the path change affect us-east-core specifically?",
              "context": "Metrics sees error rate +18% correlated with T-2min event"
            }
          }
        }
      ]
    }
  }
}
```

#### Task Response

Agents respond synchronously. The `artifact` carries the structured result:

```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "result": {
    "id": "task-metrics-INC-20260504-001",
    "sessionId": "INC-20260504-001",
    "status": {
      "state": "completed",
      "timestamp": "2026-05-04T14:31:45Z"
    },
    "artifacts": [
      {
        "name": "agent_finding",
        "parts": [
          {
            "type": "data",
            "data": {
              "agent_id": "metrics_agent",
              "domain": "metrics",
              "anomaly_detected": true,
              "summary": "Error rate exceeded 3-sigma at T-3min; latency +40ms correlated",
              "key_events": [],
              "start_time": "2026-05-04T14:01:00Z",
              "end_time": "2026-05-04T14:31:00Z",
              "confidence": 0.82,
              "revised": false,
              "revision_count": 0
            }
          }
        ]
      }
    ]
  }
}
```

---

### Task Lifecycle Mapping

ADK A2A defines a standard task lifecycle. NetCortex maps its internal phases to this lifecycle:

```
submitted   → Task received by agent endpoint, queued for processing
working     → Agent is fetching data from its DataProvider and running analysis
completed   → AgentFinding produced and returned in artifacts
failed      → DataProvider error, LLM failure after retries, or Pydantic validation failure
cancelled   → Supervisor timeout fired; task abandoned, synthesis proceeds with available findings
```

**Lifecycle state is tracked per task in the `a2a_router.py` registry.** The supervisor monitors task states during the analysis phase. If any task reaches `cancelled` state due to `analysis_timeout_seconds`, the supervisor logs the timeout and proceeds to the A2A collaboration phase with available findings only.

For peer messages during collaboration, the lifecycle is shorter:
```
submitted → working → completed   (normal path)
submitted → working → failed      (peer returned error)
submitted → cancelled             (message_timeout_seconds exceeded)
```

A `cancelled` or `failed` peer message is logged and the requesting agent proceeds without that peer's response for the current round. It does not block the round from completing.

---

### SSE Streaming — Explicit Decision

**NetCortex does not use SSE streaming. All A2A communication uses synchronous HTTP POST/response.**

Rationale:
- The A2A collaboration loop runs for a maximum of 2 rounds with a `round_timeout_seconds` ceiling
- Each individual message has a `message_timeout_seconds` boundary (default 10s)
- For 2-iteration collaboration within a 30s total budget, synchronous HTTP is sufficient and significantly simpler to implement, debug, and test
- SSE requires persistent connections and streaming parsers in both the sender and receiver; this complexity is not justified by the use case
- Agent cards explicitly declare `"streaming": false` to signal this to any third-party agent attempting to connect

**If a future use case requires long-running async tasks** (e.g., a custom agent that queries a slow external system), use the `tasks/sendSubscribe` method with a callback webhook instead of SSE. This is the ADK A2A push notification pattern and is supported by `a2a_router.py` as a future extension point, but is not required for the initial implementation.

---

## ⏱ Timeout Architecture

The current spec's single `timeout_seconds: 30` is ambiguous. NetCortex defines **four distinct timeout boundaries**, each governing a different failure mode with different handling behavior.

### The Four Timeout Boundaries

```
Phase 1: Parallel Analysis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  [metrics_agent] ─────────────────────── analysis_timeout_seconds (per agent)
  [log_agent]     ────────────────────
  [routing_agent] ─────────────────────────────────────
  [config_agent]  ─────────────────────────────────── ← if this exceeds limit → cancelled

Phase 2: A2A Collaboration Loop
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Round 1:
    [broadcast]  → [peer response]  ← message_timeout_seconds (per message)
    [peer query] → [peer response]  ← message_timeout_seconds (per message)
    ────────────────────────────── ← round_timeout_seconds (entire round ceiling)

  Round 2: (same structure)
  ──────────────────────────────────────────────────── ← collaboration_timeout_seconds (all rounds)
```

### Timeout Definitions

| Config Key | Default | Governs | On Expiry |
|---|---|---|---|
| `analysis_timeout_seconds` | 20s | How long a single domain agent analysis task may run before the supervisor marks it `cancelled` | Agent marked `timed_out` in state; synthesis proceeds with remaining agents' findings; logged in trace |
| `message_timeout_seconds` | 10s | How long an agent waits for a response to a single peer message (`clarification_request`, `validation_request`) | Message marked `cancelled`; requesting agent proceeds without that response; logged in `a2a_messages` |
| `round_timeout_seconds` | 25s | Ceiling on a single A2A collaboration round — from first broadcast to last revision | Round forced to close with whatever responses arrived; any pending peer responses discarded; revisions computed on partial input |
| `collaboration_timeout_seconds` | 60s | Total ceiling on the entire A2A loop across all rounds | Loop terminated immediately; synthesis runs on best available revised findings; timeout logged in `supervisor_state.json` |

### Timeout Relationships and Constraints

These values must satisfy the following constraints, enforced by config validation at startup:

```
message_timeout_seconds < round_timeout_seconds
round_timeout_seconds × max_iterations ≤ collaboration_timeout_seconds
analysis_timeout_seconds < collaboration_timeout_seconds
```

If the config violates any constraint, NetCortex raises a `ConfigValidationError` at startup with a clear message identifying which constraint failed.

### Updated `config.yaml`

```yaml
llm:
  provider: google
  model: gemini-2.5-flash
  api_key_env: GEMINI_API_KEY

providers:
  metrics: simulation          # or: prometheus, mcp
  logs: simulation             # or: elk, splunk, mcp
  routing: simulation          # or: custom, mcp
  config: simulation           # or: custom, mcp

mcp_endpoints:
  metrics: ""                  # e.g. http://your-prometheus-mcp:8080
  logs:    ""                  # e.g. http://your-elk-mcp:8080
  routing: ""
  config:  ""

a2a:
  max_iterations: 2
  # Four distinct timeout boundaries — see timeout architecture section for constraints
  analysis_timeout_seconds: 20      # Per domain agent: analysis phase only
  message_timeout_seconds: 10       # Per peer message: clarification/validation response wait
  round_timeout_seconds: 25         # Per collaboration round: broadcast through all revisions
  collaboration_timeout_seconds: 60 # Total A2A loop ceiling across all rounds

simulation:
  region: us-east
  window_minutes: 30
```

---

## 🔄 Agent Extensibility & Replacement

NetCortex is explicitly designed for three tiers of agent replacement. The tier you need determines how much work is required. **`AgentFinding` is the schema contract** — any agent replacing a built-in must produce this exact schema. Everything else is replaceable.

### The Schema Contract

`AgentFinding` is the single interface that NetCortex's LangGraph reducers, the A2A collaboration loop, and the RCA synthesizer all consume. It is the **public API of every domain agent**.

```python
class AgentFinding(BaseModel):
    agent_id: str
    domain: Literal["metrics", "logs", "routing", "config"]
    anomaly_detected: bool
    summary: str
    key_events: list[dict]
    start_time: datetime
    end_time: datetime
    confidence: float
    revised: bool = False
    revision_count: int = 0
```

**Changing `AgentFinding` is a breaking change.** It requires updating the reducers in `orchestrator.py`, the synthesizer's consumption logic in `rca_synthesizer.py`, and the Agent Card `schemaContract` version on every agent. Treat it as a versioned API — bump `schemaContract.version` in all Agent Cards when the schema changes.

---

### Tier 1 — Drop-In Endpoint Replacement (Same Schema, Different Implementation)

**Use case:** You have your own Metrics analysis service — maybe it queries Prometheus directly and uses a statistical model you've built internally. You want NetCortex to use your service instead of the built-in metrics agent, without touching the orchestrator.

**What you need to do:**

1. Implement your service so it serves a valid Agent Card at `GET /.well-known/agent.json` with `"outputSchema": "AgentFinding"` and `"version": "1.0.0"`
2. Implement the `tasks/send` endpoint that accepts the JSON-RPC analysis task and returns an `AgentFinding` in the `artifacts` field
3. Implement the `respond-to-peer` skill so your agent can participate in the A2A collaboration loop
4. Update `config.yaml` with your endpoint URL:

```yaml
agents:
  metrics:
    endpoint: http://your-metrics-service:9001/a2a
  log:
    endpoint: http://localhost:8002/a2a       # keep built-in
  routing:
    endpoint: http://localhost:8003/a2a       # keep built-in
  config:
    endpoint: http://localhost:8004/a2a       # keep built-in
```

**What you do NOT need to change:** `orchestrator.py`, `models/schemas.py`, any reducer, the synthesizer, or any other built-in agent. The orchestrator reads the endpoint from config, `a2a_router.py` reads the Agent Card at startup to validate the schema contract, and the LangGraph node calls it transparently.

**What `a2a_router.py` validates at startup:**
- Agent Card is reachable at `/.well-known/agent.json`
- `schemaContract.outputSchema == "AgentFinding"`
- `schemaContract.version` is compatible with the current NetCortex version
- Both required skills (`analyze-<domain>` and `respond-to-peer`) are declared

If validation fails, NetCortex raises a `AgentRegistrationError` at startup with the agent name and the specific validation failure.

---

### Tier 2 — External Agent (ADK A2A Endpoint Only, Not a LangGraph Node)

**Use case:** You have a fully external agent — maybe it runs on a different host, in a different language, or is a vendor-supplied agent that speaks ADK A2A but knows nothing about LangGraph. You want to plug it into NetCortex's collaboration graph without running it inside the LangGraph process.

**The problem:** NetCortex's domain agents are dual-role — they run as both LangGraph nodes and ADK A2A endpoints. A purely external agent is only an ADK A2A endpoint. LangGraph cannot directly invoke it as a node.

**The solution — the Adapter Node pattern:**

NetCortex provides a generic `ExternalAgentAdapterNode` in `agents/external_adapter.py`. This is a thin LangGraph node whose only job is to call an external ADK A2A endpoint and return the result to LangGraph state.

```python
class ExternalAgentAdapterNode:
    """
    Thin LangGraph node wrapper for any external ADK A2A agent.

    Handles:
    - Agent Card validation at startup
    - JSON-RPC task dispatch via a2a_router
    - analysis_timeout_seconds enforcement
    - AgentFinding schema validation on response
    - Error handling and timed_out state marking

    Usage: register in orchestrator.py instead of the built-in agent node.
    The external agent only needs to speak ADK A2A — it has no knowledge of LangGraph.
    """

    def __init__(self, agent_id: str, endpoint: str, domain: str):
        self.agent_id = agent_id
        self.endpoint = endpoint
        self.domain = domain

    def __call__(self, state: NetCortexState) -> dict:
        # Dispatch via a2a_router, enforce analysis_timeout_seconds
        # Validate response against AgentFinding schema
        # Return {findings: [AgentFinding]} for reducer merge
        ...
```

**What the external agent must provide:**
- A valid Agent Card at `/.well-known/agent.json`
- `tasks/send` JSON-RPC endpoint that returns `AgentFinding` in artifacts
- `respond-to-peer` skill for A2A collaboration participation

**What you configure in `config.yaml`:**

```yaml
agents:
  routing:
    endpoint: http://vendor-routing-agent.internal:7000/a2a
    use_external_adapter: true   # tells orchestrator to use ExternalAgentAdapterNode
```

**What you do NOT need:** The external agent does not need to import any NetCortex code, know about LangGraph, or run in the same process or even the same network zone (as long as the endpoint is reachable).

---

### Tier 3 — Schema Extension (Adding Fields to AgentFinding)

**Use case:** You need to add domain-specific fields to `AgentFinding` — for example, a custom `bgp_as_path` field in the Routing Agent's finding that the synthesizer should reason over.

**This is a breaking change. Follow this process:**

1. Add the new field to `AgentFinding` in `models/schemas.py` with `default=None` so existing agents are not broken immediately
2. Bump `schemaContract.version` in all Agent Cards that produce the new field
3. Update the synthesizer prompt to reference the new field where relevant
4. Update `merge_findings` reducer if the new field requires custom merge logic
5. Update `expected_rca_keywords` in affected scenarios in `simulation/scenarios.py`
6. Document the field addition in the changelog as a minor version bump

Fields added with `default=None` are backward-compatible. Fields without a default, or fields that change the type of an existing field, are major version bumps and require updating all agents simultaneously.

---

## 🏗 System Architecture

### High-Level Flow

```
External System (PagerDuty / ServiceNow / Jira / CLI)
        ↓
Ingestion Layer (FastAPI Webhook)
  - Receive incident payload
  - Normalize to IncidentRequest schema
  - Assign incident_id (UUID or from external system)
        ↓
LangGraph Supervisor Node
  - Parse intent
  - Classify degradation type
  - Decompose into domain tasks
  - Dispatch agents (parallel fan-out)
        ↓
┌────────────────────────────────────────────────────────────┐
│         Parallel Analysis Phase (LangGraph super-step)     │
│  All agents execute simultaneously.                        │
│  Each agent calls its ADK A2A endpoint via JSON-RPC.       │
│  analysis_timeout_seconds enforced per agent.              │
├──────────────┬───────────┬─────────────┬───────────────────┤
│ Metrics      │ Log       │ Routing     │ Config            │
│ Agent Node   │ Agent Node│ Agent Node  │ Agent Node        │
│ (or External │           │             │                   │
│  Adapter)    │           │             │                   │
└──────┬───────┴─────┬─────┴──────┬──────┴─────┬─────────────┘
       └─────────────┴────────────┴────────────┘
                ↓ reducer: merge_findings
        LangGraph State: findings[]

        ↓
┌────────────────────────────────────────────────────────────┐
│       A2A Collaboration Subgraph (LangGraph)               │
│                                                            │
│  Round-based: collect ALL peer responses first,           │
│  then each agent revises once per round.                   │
│                                                            │
│  message_timeout_seconds: per peer message                 │
│  round_timeout_seconds:   per round ceiling                │
│  collaboration_timeout_seconds: total loop ceiling         │
│                                                            │
│  Termination:                                              │
│    1. All peers anomaly_detected=false (short-circuit)     │
│    2. No finding_update issued (stable state)              │
│    3. max_iterations reached                               │
│    4. collaboration_timeout_seconds exceeded               │
└────────────────────────────────────────────────────────────┘
        ↓ reducer: merge_findings (revised)
        LangGraph State: revised_findings[]

        ↓
LangGraph RCA Synthesizer Node
  - Collect all final AgentFindings from revised_findings in state
  - LLM builds causal chain and root cause narrative
  - Code computes confidence score deterministically
  - Emits final RCAReport
        ↓
Final RCA Report (JSON + trace files)
        ↓
Action Layer (future)
```

### LangGraph Graph Definition (Macro View)

```
[START]
    │
    ▼
[supervisor_node]              ← LLM: parse, classify, scope agents
    │
    ▼
[parallel fan-out]             ← LangGraph super-step boundary
  ├── [metrics_agent_node]     ← built-in or ExternalAgentAdapterNode
  ├── [log_agent_node]
  ├── [routing_agent_node]
  └── [config_agent_node]
    │
    ▼ reducer: merge_findings
[a2a_collaboration_node]       ← subgraph: round-based peer exchange
    │
    ▼ reducer: merge_findings (revised)
[rca_synthesizer_node]         ← LLM narrative + code confidence score
    │
    ▼
[END]
```

---

## 🧩 Core Components

### 1. Supervisor Agent (LangGraph Node)

Responsibilities:
- Parse the incident description using LLM
- Classify the type of degradation (network / config / overload / unknown)
- Determine which domain agents are relevant
- Dispatch agents in parallel via LangGraph fan-out
- Manage global workflow state (LangGraph `StateGraph`)
- Enforce `analysis_timeout_seconds` per dispatched agent
- Transition to A2A collaboration phase once all agents report (or time out)
- Transition to synthesis phase after A2A loop completes or `collaboration_timeout_seconds` exceeded

---

### 2. Domain Agents (LangGraph Nodes + ADK A2A Endpoints)

Each built-in domain agent serves **dual roles**:
1. As a **LangGraph node** — receives state, fetches data, produces `AgentFinding`, returns to LangGraph state via reducer
2. As an **ADK A2A HTTP endpoint** — serves Agent Card at `/.well-known/agent.json`, accepts JSON-RPC `tasks/send` for analysis and peer messages

Third-party or external agents only need to serve the ADK A2A endpoint. The `ExternalAgentAdapterNode` provides the LangGraph node wrapper.

#### Metrics Agent
- Analyzes: latency, error rate, packet loss, throughput
- Detects deviation from rolling baseline
- Flags anomaly windows with timestamps
- Endpoint: `http://localhost:8001/a2a`

#### Log Agent
- Processes: error logs, service exceptions, timeout patterns
- Extracts anomaly clusters and event frequency spikes
- Identifies correlated error bursts
- Endpoint: `http://localhost:8002/a2a`

#### Routing Agent
- Analyzes: network path changes, congestion indicators, BGP/routing updates
- Detects topology changes or rerouting events
- Timestamps path change events
- Endpoint: `http://localhost:8003/a2a`

#### Config Agent
- Tracks: bandwidth policies, system updates, configuration drift
- Identifies operational changes (deployments, policy pushes) proximate to incident time
- Flags change events with before/after diff
- Endpoint: `http://localhost:8004/a2a`

---

### 3. A2A Collaboration Loop (LangGraph Subgraph + Google ADK A2A)

This is the core differentiator of NetCortex. Rather than agents only reporting upward to the supervisor, agents **communicate laterally** via the ADK A2A protocol using JSON-RPC over HTTP POST.

#### Concurrency Model — Round-Based, Collect-Then-Revise

**Do NOT** allow agents to revise immediately upon receiving each peer response. If an agent revises mid-round, subsequent peers see an inconsistent view. This is the stale-snapshot race condition.

**Additional runtime rule:** during parallel analysis, agents queue inbound `respond-to-peer` requests and do not process them until the collaboration phase starts. This prevents analysis-phase race conditions where slow agents receive peer requests before their first finding is finalized.

**Correct model:**
```
Round N start:
  Each agent broadcasts its current finding (finding_publish) via JSON-RPC tasks/send
  Each agent sends targeted clarification/validation requests as needed

Wait: collect ALL peer responses for ALL agents
      → Governed by message_timeout_seconds per message
      → Governed by round_timeout_seconds as the ceiling for the entire round

Round N end (synchronization point):
  Each agent reviews ALL peer responses received this round
  Each agent revises its finding ONCE (finding_update) — or issues no revision
  Revised findings written to LangGraph state via merge_findings reducer

→ Check termination conditions
→ If not terminal: Round N+1
```

#### Termination Conditions (evaluated after each round, in priority order)
1. **False positive short-circuit**: All peers return `anomaly_detected: false` in round 1 → terminate immediately
2. **Stable state**: No agent issued a `finding_update` in the last round
3. **Max iterations reached**: Round count == `max_iterations` (default 2)
4. **Collaboration timeout**: `collaboration_timeout_seconds` exceeded → force synthesis immediately

#### A2A Message Types
| Message Type | JSON-RPC Skill | Purpose |
|---|---|---|
| `finding_publish` | `respond-to-peer` | Agent broadcasts current-round finding to all peers |
| `clarification_request` | `respond-to-peer` | Agent asks a peer for detail on a specific signal |
| `clarification_response` | `respond-to-peer` | Peer responds to clarification request |
| `validation_request` | `respond-to-peer` | Agent asks a peer to confirm or reject a hypothesis |
| `validation_response` | `respond-to-peer` | Peer confirms or rejects |
| `finding_update` | `respond-to-peer` | Agent's revised finding after processing all peer input |

#### Example Interaction
```
[Round 1 — Broadcast phase]
Metrics  →[broadcast / finding_publish]: "Error rate +18%, latency +40ms since T-3min"
Routing  →[broadcast / finding_publish]: "us-east-core path changed at T-2min"
Config   →[broadcast / finding_publish]: "Bandwidth throttle applied at T-5min"
Log      →[broadcast / finding_publish]: "No correlated error burst — timeout pattern only"

[Round 1 — Clarification phase (same round, not acted on until round ends)]
Metrics  →[Routing / clarification_request]: "Did the path change affect us-east-core?"
Routing  →[Metrics / clarification_response]: "Yes, rerouted to congested path"

--- message_timeout_seconds enforced per exchange above ---
--- round_timeout_seconds ceiling covers entire Round 1 ---

[Round 1 end — synchronization point. All responses collected.]
Metrics  →[broadcast / finding_update]: "Severity elevated — routing change corroborated"

[Stable state check: no further finding_update → terminate]
```

### 4. RCA Synthesizer Agent (LangGraph Node)

Responsibilities:
- Collect all final `AgentFinding` objects from `revised_findings` in LangGraph state
- Build a unified timeline of events across all domains
- LLM constructs causal chain and root cause narrative
- **Code** (not LLM) computes confidence score deterministically using the formula below
- Emits final `RCAReport`

#### Confidence Score Formula
```
confidence = (corroborating_signals / total_signals)
             × domain_weight_factor(n_agreeing_domains)
             × (1 - conflict_penalty)
```

**`domain_weight_factor` lookup table:**

| Agreeing Domains | `domain_weight_factor` |
|---|---|
| 1 | 0.50 |
| 2 | 0.75 |
| 3 | 0.90 |
| 4 | 1.00 |
 
**`conflict_penalty` lookup table:**

| Conflict State | `conflict_penalty` |
|---|---|
| No conflicts | 0.00 |
| Minor (1 agent disagrees, others agree) | 0.10 |
| Major (2-2 split or 3-1 with strong disagreement) | 0.25 |
| Unresolvable (contradictory root cause claims) | 0.40 |

**Worked example:**
- 3 domains corroborate routing change, 1 domain (logs) has no corroborating signal, no conflicts
- `confidence = (3/4) × 0.90 × 1.00 = 0.675` → `0.68`

The LLM never computes or outputs the confidence score. The synthesizer LLM returns only the narrative and causal chain. The score is computed from the structured `AgentFinding` objects in state by code.

---

## 📦 Project File Structure

```
net_cortex/
├── agents/
│   ├── supervisor.py               # LangGraph supervisor node
│   ├── metrics_agent.py            # LangGraph node + ADK A2A endpoint (port 8001)
│   ├── log_agent.py                # LangGraph node + ADK A2A endpoint (port 8002)
│   ├── routing_agent.py            # LangGraph node + ADK A2A endpoint (port 8003)
│   ├── config_agent.py             # LangGraph node + ADK A2A endpoint (port 8004)
│   ├── external_adapter.py         # ExternalAgentAdapterNode for Tier 2 replacement
│   └── rca_synthesizer.py          # LangGraph synthesis node
├── communication/
│   ├── a2a_router.py               # Agent Card discovery, JSON-RPC dispatch, timeout enforcement
│   ├── message_types.py            # ADK A2A Pydantic schemas (A2AMessage, TaskRequest, TaskResponse)
│   └── agent_registry.py           # Runtime registry: agent_id → {endpoint, card, status}
├── ingestion/
│   ├── webhook_server.py           # FastAPI HTTP server — receives incidents
│   └── incident_normalizer.py      # Normalizes PagerDuty / ServiceNow / Jira / custom payloads
├── scripts/
│   └── send_incident.py            # Dev/test helper: send incident payloads to webhook over HTTP
├── providers/
│   ├── base.py                     # Abstract DataProvider interfaces
│   ├── simulation/
│   │   ├── metrics_sim.py
│   │   ├── log_sim.py
│   │   ├── routing_sim.py
│   │   └── config_sim.py
│   └── adapters/
│       ├── prometheus_adapter.py
│       ├── elk_adapter.py
│       ├── splunk_adapter.py
│       └── mcp_adapter.py
├── models/
│   └── schemas.py                  # All Pydantic models — the schema contract
├── simulation/
│   └── scenarios.py                # Scenarios 1–10
├── actions/
│   ├── base.py
│   ├── ticket_updater.py
│   ├── notifier.py
│   ├── remediation_trigger.py
│   └── escalation_handler.py
├── orchestrator.py                 # LangGraph StateGraph, NetCortexState, reducers
├── config.yaml
├── main.py
├── requirements.txt                # All dependencies pinned
├── tests/
│   ├── unit/                       # Fast, isolated unit tests (default local run)
│   ├── integration/                # Multi-component behavior tests
│   └── e2e/                        # End-to-end workflow tests (scenarios, timeouts, short-circuit)
└── README.md
```

---

## 🔌 Data Provider Abstraction

### Abstract Interface (`providers/base.py`)
```python
class MetricsProvider(ABC):
    @abstractmethod
    def get_metrics(self, region: str, window_minutes: int) -> list[MetricSnapshot]: ...

class LogProvider(ABC):
    @abstractmethod
    def get_logs(self, region: str, window_minutes: int) -> list[LogEvent]: ...

class RoutingProvider(ABC):
    @abstractmethod
    def get_routing_events(self, region: str, window_minutes: int) -> list[RoutingEvent]: ...

class ConfigProvider(ABC):
    @abstractmethod
    def get_config_changes(self, region: str, window_minutes: int) -> list[ConfigChange]: ...
```

### MCP Integration Strategy
- The `mcp_adapter.py` stub implements the `DataProvider` interface by calling a user-provided MCP server URL
- Google ADK natively supports MCP tool invocation — domain agents can call MCP tools directly when the `mcp` provider is selected
- Users point the config at their own MCP servers — no changes to agent logic required
- Simulation is the default and always works without any external services

---

## 📊 Data Models (`schemas.py`)

```python
from __future__ import annotations
from datetime import datetime
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field
import uuid


class IncidentRequest(BaseModel):
    incident_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Primary identifier for this RCA run. Auto-generated as UUID if not supplied."
    )
    scenario_id: int | None = Field(
        default=None,
        description="Built-in simulation scenario number (1–10). None for real incidents."
    )
    description: str
    region: str
    reported_at: datetime
    severity: Literal["low", "medium", "high", "critical"]
    source_system: str | None = None
    external_incident_id: str | None = None


class MetricSnapshot(BaseModel):
    timestamp: datetime
    region: str
    error_rate: float
    packet_loss: float
    throughput_gbps: float
    latency_ms: float


class LogEvent(BaseModel):
    timestamp: datetime
    level: Literal["INFO", "WARN", "ERROR", "FATAL"]
    service: str
    message: str


class RoutingEvent(BaseModel):
    timestamp: datetime
    region: str
    path_id: str
    change_type: Literal["reroute", "flap", "congestion", "bgp_update"]
    details: str


class ConfigChange(BaseModel):
    timestamp: datetime
    component: str
    change_type: Literal["policy_update", "deployment", "bandwidth_limit", "rollback"]
    before: dict
    after: dict


class AgentFinding(BaseModel):
    """
    The schema contract for all domain agents.

    Any agent — built-in or third-party — that participates in the NetCortex
    A2A collaboration loop must produce exactly this schema. This is enforced
    at startup via Agent Card schemaContract validation and at runtime via
    Pydantic parsing of the JSON-RPC artifact payload.

    Schema version: 1.0.0
    Breaking change policy: adding optional fields (default=None) is minor;
    adding required fields or changing existing field types is major.
    """
    agent_id: str
    domain: Literal["metrics", "logs", "routing", "config"]
    anomaly_detected: bool
    summary: str
    key_events: list[dict]
    start_time: datetime = Field(description="Start of the analysis window")
    end_time: datetime = Field(description="End of the analysis window")
    confidence: float
    revised: bool = False
    revision_count: int = 0


class A2AMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender_agent: str
    target_agent: Union[Literal["broadcast"], str] = Field(
        description="'broadcast' for round-wide messages. Agent ID for directed messages."
    )
    message_type: Literal[
        "finding_publish",
        "clarification_request",
        "clarification_response",
        "validation_request",
        "validation_response",
        "finding_update"
    ]
    payload: dict
    timestamp: datetime
    round_number: int = Field(
        description="Collaboration round this message belongs to. "
                    "Agents only act on messages matching the current round number."
    )


class RCAReport(BaseModel):
    incident_id: str
    root_cause: str
    contributing_factors: list[str]
    causal_chain: list[str]
    metrics_affected: list[str]
    agent_findings: list[AgentFinding]
    a2a_message_log: list[A2AMessage]
    confidence_score: float = Field(
        description="Computed deterministically by code after LLM synthesis. Never set by LLM."
    )
    corroborating_domain_count: int
    conflict_detected: bool
    generated_at: datetime
```

---

## 📊 LangGraph State Schema and Reducers

```python
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, END


def merge_findings(
    existing: list[AgentFinding],
    new: list[AgentFinding]
) -> list[AgentFinding]:
    """
    Upsert by agent_id. Newer finding replaces existing for the same agent_id.
    Used for both initial analysis fan-out and A2A revised findings.
    """
    existing_map = {f.agent_id: f for f in existing}
    for finding in new:
        existing_map[finding.agent_id] = finding
    return list(existing_map.values())


def merge_a2a_messages(
    existing: list[A2AMessage],
    new: list[A2AMessage]
) -> list[A2AMessage]:
    """
    Append-only. Dedup by message_id to handle retries safely.
    """
    existing_ids = {m.message_id for m in existing}
    return existing + [m for m in new if m.message_id not in existing_ids]


class NetCortexState(TypedDict):
    incident: IncidentRequest
    degradation_type: str
    active_agents: list[str]
    findings: Annotated[list[AgentFinding], merge_findings]
    revised_findings: Annotated[list[AgentFinding], merge_findings]
    a2a_messages: Annotated[list[A2AMessage], merge_a2a_messages]
    collaboration_round: int
    collaboration_complete: bool
    timed_out_agents: list[str]    # agents that hit analysis_timeout_seconds
    rca_report: RCAReport | None
```

---

## 📉 Simulation Design

NetCortex ships with a realistic simulation layer. All data is synthetic but statistically modeled on real distributed system behavior.

### Baseline System State
| Metric | Healthy Baseline |
|---|---|
| Error Rate | 0.5% |
| Packet Loss Rate | 0.2% |
| Throughput | 1 Gbps |
| Latency | 50 ms |

### Simulation Implementation Notes

```python
class ScenarioDataBundle(BaseModel):
    scenario_id: int
    scenario_name: str
    metrics_data: list[MetricSnapshot]
    log_events: list[LogEvent]
    routing_events: list[RoutingEvent]
    config_changes: list[ConfigChange]
    incident_request: IncidentRequest
    expected_rca_keywords: list[str]

SCENARIOS: dict[int, ScenarioDataBundle] = {
    1: build_scenario_1(),
    ...
    10: build_scenario_10(),
}
```

Each provider receives only its own domain's data for the active scenario — preserving the partial-truth property that makes A2A necessary.

---

## 📊 Simulation Scenarios — Data Specification

### Scenario 1 — Port Capacity Reduction
| Provider | Injected Data |
|---|---|
| **MetricsSim** | Switches A, B, D: ~1 Gbps each. Switch C: ~500 Mbps. PLR elevated on GW aggregate |
| **LogSim** | Policy event T-5min: `"Policy QOS-GW-V3 applied"` — no interface name |
| **RoutingSim** | No events |
| **ConfigSim** | T-5min: component=`"Switch-C eth0/1"`, change_type=`"bandwidth_limit"`, before=`{capacity_gbps:10}`, after=`{capacity_gbps:5}` |

### Scenario 2 — BGP Route Withdrawal
| Provider | Injected Data |
|---|---|
| **MetricsSim** | PLR 2.8% on dst_prefix=`192.0.2.0/24` only. All other prefixes normal |
| **LogSim** | T-3min: `"BGP session flap — peer 203.0.113.1"` — no prefix detail |
| **RoutingSim** | T-3min: path_id=`"192.0.2.0/24"`, change_type=`"bgp_update"`, details=`"Peer withdrew prefix, rerouted via backup +3 hops"` |
| **ConfigSim** | No events |

### Scenario 3 — QoS Priority Queue Starvation
| Provider | Injected Data |
|---|---|
| **MetricsSim** | Latency 250ms on DSCP=EF flows only. Best-effort 48ms. Throughput unchanged |
| **LogSim** | T-8min: `"Policy QOS-CORE-V2 applied to core interfaces"` — name only |
| **RoutingSim** | No events |
| **ConfigSim** | T-8min: component=`"QOS-CORE-V2"`, before=`{ef_queue_pct:30}`, after=`{ef_queue_pct:5}` |

### Scenario 4 — LAG Member Link Failure
| Provider | Injected Data |
|---|---|
| **MetricsSim** | LAG0 throughput 20→10 Gbps. PLR 0.6%. Aggregate interface still up |
| **LogSim** | T-2min: `"LACP PDU timeout on LAG0 member eth1/2 — removed from bundle"` |
| **RoutingSim** | No routing changes — LAG0 aggregate still up at L3 |
| **ConfigSim** | No changes |

### Scenario 5 — ACL Change Blocking Traffic
| Provider | Injected Data |
|---|---|
| **MetricsSim** | Error rate 18% on flows from `10.20.0.0/16`. All other ranges 0.5% |
| **LogSim** | High volume ACL deny: `"ACL EDGE-INBOUND-V4 deny src=10.20.0.0/16"` + TCP RST errors |
| **RoutingSim** | No events |
| **ConfigSim** | T-10min: new deny rule for `10.20.0.0/16` added to EDGE-INBOUND-V4 |

### Scenario 6 — MTU Mismatch (Fragmentation Drops)
| Provider | Injected Data |
|---|---|
| **MetricsSim** | PLR 3.1% on flows > 1500 bytes. Small-packet PLR 0.2% |
| **LogSim** | T-15min: `"Fragmentation needed, DF bit set — eth2/0"` at high frequency |
| **RoutingSim** | No events |
| **ConfigSim** | T-15min: component=`"eth2/0"`, before=`{mtu:9000}`, after=`{mtu:1500}` |

### Scenario 7 — NTP Drift (Auth Failures)
| Provider | Injected Data |
|---|---|
| **MetricsSim** | Error rate ~6% equally across services A, B, C. Latency normal |
| **LogSim** | T-30min: `"Kerberos ticket validation failed — clock skew exceeds 5 minutes"` across all services |
| **RoutingSim** | No events |
| **ConfigSim** | T-30min: NTP changed from internal stratum-2 to `pool.ntp.org` |

### Scenario 8 — STP Broadcast Storm
| Provider | Injected Data |
|---|---|
| **MetricsSim** | Throughput collapsed, CPU 95%+ on core switches. All traffic types affected |
| **LogSim** | STP TCN from SW-ACC-07 repeated. MAC flush every 30s. `"SW-ACC-07 elected STP root"` |
| **RoutingSim** | L3 stable. Note: `"L2 forwarding instability on access layer"` |
| **ConfigSim** | T-20min: SW-ACC-07 deployed with default STP priority 32768 (not explicitly set) |

### Scenario 9 — TE Tunnel Reoptimization (Transient Blackhole)
| Provider | Injected Data |
|---|---|
| **MetricsSim** | PLR 12%, latency 280ms from T+0 to T+2min. Baseline after T+2min |
| **LogSim** | T+0: `"TE-CORE-01 path teardown (CSPF reoptimization)"`. T+2min: `"re-established via new path"`. No make-before-break entry |
| **RoutingSim** | T+0: TE-CORE-01 path A→B→D changed to A→C→D. Convergence 118 seconds |
| **ConfigSim** | Auto CSPF: `{trigger:"automatic", make_before_break:false}` |

### Scenario 10 — Noisy Baseline (False Positive Suppression)
| Provider | Injected Data |
|---|---|
| **MetricsSim** | Error rate 0.6% (baseline 0.5%) — within 1-sigma. All others nominal |
| **LogSim** | No errors. Normal INFO logs only |
| **RoutingSim** | No events |
| **ConfigSim** | No events |

> **Critical:** This scenario must trigger the false-positive short-circuit. All three peers return `anomaly_detected: false` in round 1. The loop must terminate after round 1 without proceeding to round 2. This is a required automated test — not just a documentation note.

---

## 🔍 Scenario Analysis — A2A Collaboration Value Proofs

### Scenario 1
**Expected RCA:** Policy on Port C reduced capacity 10G→5G causing gateway PLR shift. Confidence: high.
- Metrics → Config: path change identified as config-driven, not hardware
- Log → Config: policy anchored to Port C specifically

### Scenario 2
**Expected RCA:** BGP peer withdrew 192.0.2.0/24, rerouted to congested backup path, selective PLR. Confidence: high.
- Metrics → Routing: prefix-specific PLR confirmed as routing event
- Log → Routing: BGP flap anchored to specific peer and prefix

### Scenario 3
**Expected RCA:** QoS EF queue reduced 30%→5%, starving priority traffic. Confidence: high.
- Metrics → Config: EF latency spike anchored to queue config change
- Log → Config: policy diff obtained for synthesizer

### Scenario 4
**Expected RCA:** LACP timeout removed eth1/2 from LAG0, halved capacity 20G→10G. Confidence: high.
- Metrics → Log: throughput halving explained by member link event
- Routing → Log: partial LAG failure pattern identified

### Scenario 5
**Expected RCA:** ACL deny rule for 10.20.0.0/16 added to EDGE-INBOUND-V4. Confidence: high.
- Metrics → Log: active drops confirmed (not timeouts)
- Log → Config: ACL change matched to deny entries

### Scenario 6
**Expected RCA:** MTU reduced 9000→1500 on eth2/0, large packets fragmented and dropped. Confidence: high.
- Metrics → Log: size-dependent PLR correlated to ICMP fragmentation errors
- Log → Config: MTU change on eth2/0 at exact start timestamp

### Scenario 7
**Expected RCA:** NTP server change caused clock drift, Kerberos/TLS time-validation failures. Confidence: high.
- Log → Config: timestamp anomalies linked to NTP change
- Metrics → Log: distributed failure pattern identified as common dependency

### Scenario 8
**Expected RCA:** SW-ACC-07 deployed without STP priority, won root election, broadcast storm. Confidence: high.
- Metrics → Log: CPU/throughput collapse linked to STP TCN storm
- Log → Config: new switch deployment without STP priority

### Scenario 9
**Expected RCA:** TE-CORE-01 reoptimized without make-before-break, 2-minute blackhole. Confidence: high.
- Metrics → Routing: 2-minute spike window matches tunnel convergence window
- Log → Routing: make-before-break absence confirmed as cause of blackhole

### Scenario 10
**Expected RCA:** No incident. Within-baseline noise. All peer agents return `anomaly_detected: false`. A2A loop short-circuits after round 1. Confidence in no-incident: high.

---

## 🧪 Execution Flow

### Step 1: Incident Input
```
POST /incidents
{
  "description": "High error rate and throughput drop in us-east region",
  "region": "us-east",
  "severity": "high",
  "source_system": "pagerduty",
  "external_incident_id": "PD-193842"
}
```
Ingestion assigns `incident_id` (UUID) and passes to LangGraph pipeline.

### Step 2: Supervisor
- LLM parses incident, classifies degradation type
- Sets `active_agents` in state
- Dispatches parallel fan-out

### Step 3: Parallel Analysis (LangGraph super-step)
Each agent simultaneously:
- JSON-RPC `tasks/send` dispatched to its ADK A2A endpoint
- `analysis_timeout_seconds` enforced per agent
- Returns `AgentFinding` → merged via `merge_findings` reducer
- Timed-out agents added to `timed_out_agents` in state

### Step 4: A2A Collaboration (LangGraph subgraph)
Per round:
1. Each agent broadcasts via `finding_publish`
2. Targeted `clarification_request` / `validation_request` messages sent
3. `message_timeout_seconds` enforced per message
4. `round_timeout_seconds` ceiling on entire round
5. All responses collected (synchronization point)
6. Each agent revises once → `merge_findings` reducer
7. Termination check (priority order: short-circuit → stable → max-iter → collab-timeout)

### Step 5: Synthesis
- LLM builds causal chain narrative from `revised_findings`
- Code computes confidence score using formula + lookup tables
- `RCAReport` emitted

### Step 6: Output
- `output/<incident_id>/rca_report.json`
- `output/<incident_id>/agent_trace.jsonl`
- `output/<incident_id>/a2a_messages.jsonl`
- `output/<incident_id>/supervisor_state.json`
- Human-readable console summary

---

## 📡 Incident Ingestion & External Integration

### Webhook
```
POST http://netcortex-host:8000/incidents
Content-Type: application/json
```

### Webhook Security (Deferred / Optional)

For the current open-source implementation guide, webhook authentication is intentionally **not required**.

Future adopters can optionally add:
- HMAC signature verification
- API key or token auth
- Replay protection (`X-Request-Timestamp` + nonce cache)
- IP allowlists

### Incident Injection Script

For local testing and CI, use a separate sender script instead of manually crafting HTTP calls:

```bash
python scripts/send_incident.py --url http://localhost:8000/incidents --scenario 1
python scripts/send_incident.py --url http://localhost:8000/incidents --file examples/incidents/pd_event.json
```

The script supports:
- built-in scenario payload generation
- raw payload file mode
- response capture for regression tests

### Supported Source Systems
| System | Integration Type |
|---|---|
| PagerDuty | Webhook (Event Orchestration) |
| ServiceNow | Webhook / REST API |
| Jira Service Management | Webhook |
| Custom / Generic | Raw JSON POST |
| CLI | Direct string input (`main.py`) |

---

## 🛡 Error Handling & Resilience

| Failure Mode | Handling Strategy |
|---|---|
| Agent analysis times out (`analysis_timeout_seconds`) | Agent marked `timed_out` in state; added to `timed_out_agents`; synthesis proceeds with available findings |
| Peer message times out (`message_timeout_seconds`) | Message marked `cancelled`; requesting agent proceeds without that response; logged in `a2a_messages` |
| Round times out (`round_timeout_seconds`) | Round closes with partial responses; revisions computed on available input |
| Collaboration times out (`collaboration_timeout_seconds`) | A2A loop terminated; synthesis runs immediately on best available findings |
| Agent Card invalid at startup | `AgentRegistrationError` raised; startup fails with clear message |
| Agent returns invalid schema | Pydantic validation failure; agent retries with fallback prompt (max 2); marks `timed_out` if retries fail |
| LLM malformed output | Pydantic catches; retry with fallback prompt (max 2) |
| All agents return no anomaly | RCAReport with `root_cause: "No anomaly detected"`, low confidence |
| A2A produces unresolved conflict | `conflict_penalty` applied; `conflict_detected: true` in RCAReport |
| MCP endpoint unreachable | Falls back to simulation provider; warning logged |
| Config constraint violated | `ConfigValidationError` at startup; clear message identifying failed constraint |

---

## 📊 Output Format

```json
{
  "incident_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "root_cause": "Routing path change to congested link combined with bandwidth throttling caused cascading congestion in us-east",
  "contributing_factors": [
    "Bandwidth throttle policy applied at T-5min",
    "Routing path us-east-core rerouted at T-2min",
    "Packet loss spike 0.2%→4.8% correlated with routing change",
    "Error rate 0.5%→8.1% followed routing + config events"
  ],
  "causal_chain": [
    "T-5min: Bandwidth throttle policy applied to us-east-core",
    "T-2min: Routing path changed to already-congested link",
    "T-1min: Packet loss begins spiking",
    "T+0min: Error rate and latency cross alert threshold"
  ],
  "metrics_affected": ["error_rate", "latency", "throughput", "packet_loss"],
  "confidence_score": 0.87,
  "corroborating_domain_count": 3,
  "conflict_detected": false,
  "generated_at": "2026-05-04T14:32:00Z"
}
```

---

## 🔍 Observability — Agent Trace Logging

```
output/
└── f47ac10b-58cc-4372-a567-0e02b2c3d479/
    ├── rca_report.json
    ├── agent_trace.jsonl
    ├── a2a_messages.jsonl
    └── supervisor_state.json
```

### Agent Trace Entry Format
```json
{
  "timestamp": "2026-05-04T14:31:45Z",
  "agent": "metrics_agent",
  "phase": "analysis",
  "action": "anomaly_detected",
  "detail": "Error rate exceeded 3-sigma at T-3min",
  "data_ref": "metrics_snapshot_14:28-14:31"
}
```

---

## 📦 Open-Source Packaging

### What Ships in the Repo
- Full working simulation (no external dependencies needed)
- 10 pre-built incident scenarios with A2A interaction documentation
- `ExternalAgentAdapterNode` for Tier 2 agent replacement
- Stub adapters for Prometheus, ELK, Splunk, MCP
- `config.yaml` with comments on every field including timeout constraints
- `README.md` with quickstart, architecture diagram, all three replacement tiers documented
- `requirements.txt` with all dependencies pinned (including `google-adk`)

### What Users Must Provide to Go Production
- LLM API key (Google Gemini or alternative)
- Implemented adapter(s) for their data backends (or MCP server URLs)
- Nothing else

### Extensibility Points
| Extensibility | How | Constraint |
|---|---|---|
| Replace a built-in agent endpoint | Change URL in `config.yaml` | Must serve valid Agent Card + AgentFinding schema |
| Plug in external agent (not LangGraph-native) | Set `use_external_adapter: true` in config | Must serve valid Agent Card + AgentFinding schema |
| Add new domain agent | Implement DataProvider + ADK A2A endpoint + register as LangGraph node | Must produce AgentFinding |
| Extend AgentFinding schema | Add optional field (default=None) + bump schemaContract version | Breaking if field is required or changes existing type |
| Swap LLM | Change `config.yaml` | No code changes required |
| Add scenarios | Add to `simulation/scenarios.py` | No agent code changes |

---

## ✅ Implementation Checklist

### Step 1 — Foundation
- [ ] `models/schemas.py` — All Pydantic models with docstrings (`IncidentRequest` with `incident_id`+`scenario_id`, `AgentFinding` with `start_time`/`end_time`+`revision_count`, `A2AMessage` with `round_number`+typed `target_agent`, `RCAReport` with `corroborating_domain_count`+`conflict_detected`)
- [ ] `orchestrator.py` — `NetCortexState` TypedDict with all reducer annotations; `merge_findings` and `merge_a2a_messages` implementations; `timed_out_agents` field
- [ ] `providers/base.py` — Abstract DataProvider interfaces
- [ ] `config.yaml` — All four timeout keys with documented constraints; agent endpoint overrides section
- [ ] `requirements.txt` — All dependencies pinned including `google-adk==<version>`
- [ ] Config constraint validation at startup: `message_timeout < round_timeout`, `round_timeout × max_iterations ≤ collaboration_timeout`, `analysis_timeout < collaboration_timeout`
- [ ] `tests/` structure created (`unit/`, `integration/`, `e2e/`) with pytest markers and baseline test config

### Step 2 — Simulation Layer
- [ ] `providers/simulation/metrics_sim.py` — accepts `scenario_id`
- [ ] `providers/simulation/log_sim.py` — accepts `scenario_id`
- [ ] `providers/simulation/routing_sim.py` — accepts `scenario_id`
- [ ] `providers/simulation/config_sim.py` — accepts `scenario_id`
- [ ] `simulation/scenarios.py` — Scenarios 1–10; each `IncidentRequest` includes `scenario_id`

### Step 3 — Incident Ingestion Layer
- [ ] `ingestion/webhook_server.py` — FastAPI; assigns `incident_id` on ingestion
- [ ] `ingestion/incident_normalizer.py` — Normalizes PagerDuty, ServiceNow, Jira, custom; maps `external_incident_id`
- [ ] `scripts/send_incident.py` — separate helper script for HTTP incident injection in local/dev/CI flows
- [ ] (Optional future) Add webhook security middleware: HMAC/API-key validation, replay protection, payload size limits

### Step 4 — A2A Communication Layer *(implement before agents)*
- [ ] `communication/agent_registry.py` — Runtime registry: `agent_id → {endpoint, agent_card, status, last_seen}`. Reads Agent Cards at startup via `GET /.well-known/agent.json`. Validates `schemaContract.outputSchema == "AgentFinding"` and required skills present. Raises `AgentRegistrationError` on failure
- [ ] `communication/message_types.py` — `A2AMessage` Pydantic schema; JSON-RPC `TaskRequest` and `TaskResponse` schemas matching ADK A2A wire format
- [ ] `communication/a2a_router.py` — `broadcast(finding, round_number)`: sends `finding_publish` to all registered peers via JSON-RPC `tasks/send`. `send_direct(sender, target, message_type, payload, round_number)`: directed peer message. `message_timeout_seconds` enforced on all sends. Failed/timed-out messages logged to `a2a_messages` with `cancelled` status
- [ ] A2A collaboration loop: round-based collect-then-revise; all four termination conditions (short-circuit, stable, max-iter, collab-timeout); `round_timeout_seconds` ceiling per round; `collaboration_timeout_seconds` ceiling across all rounds
- [ ] Unit tests: short-circuit fires for Scenario 10 pattern; stable-state fires when no revisions; `message_timeout_seconds` correctly cancels slow peers; config constraint validation raises on violation

### Step 5 — Agent Implementation *(after Step 4)*
- [ ] Each built-in agent (`metrics_agent.py`, `log_agent.py`, `routing_agent.py`, `config_agent.py`):
  - Serves Agent Card at `GET /.well-known/agent.json` (matching the spec above)
  - Implements `tasks/send` JSON-RPC endpoint for `analyze-<domain>` skill
  - Implements `respond-to-peer` skill for collaboration messages
  - LangGraph node function calls the built-in ADK A2A endpoint via self-HTTP and returns `AgentFinding`
  - During analysis phase, queues inbound peer requests; drains queue only in collaboration phase
- [ ] `agents/external_adapter.py` — `ExternalAgentAdapterNode`: validates Agent Card at init, dispatches JSON-RPC `tasks/send`, enforces `analysis_timeout_seconds`, validates response against `AgentFinding` schema, returns `{findings: [AgentFinding]}` for reducer
- [ ] `agents/supervisor.py` — LangGraph node; LLM classify + scope; sets `active_agents`; enforces `analysis_timeout_seconds` via async dispatch with timeout
- [ ] `agents/rca_synthesizer.py` — LangGraph node; LLM narrative only; confidence score computed by code using formula + lookup tables; `conflict_detected` set by code before LLM call

### Step 6 — Orchestration & Wiring
- [ ] Complete `orchestrator.py` — LangGraph `StateGraph`: supervisor → parallel fan-out (built-in nodes or `ExternalAgentAdapterNode` per config) → A2A collaboration subgraph → synthesizer → END
- [ ] Config-driven node selection: if `use_external_adapter: true` for an agent, wire `ExternalAgentAdapterNode`; otherwise wire built-in node
- [ ] Unit test: two agents write simultaneously → both findings preserved in state (reducer correctness)
- [ ] Unit test: all three A2A termination paths tested independently (short-circuit, stable, max-iter)
- [ ] `main.py` — starts ingestion + built-in agent endpoints, waits for health-check readiness, then runs CLI pipeline; prints report + writes output files
- [ ] Test execution split: unit tests as separate process (`pytest -m unit`), integration/e2e as separate process (`pytest -m "integration or e2e"`)

### Step 7 — Adapter Stubs
- [ ] `providers/adapters/prometheus_adapter.py`
- [ ] `providers/adapters/elk_adapter.py`
- [ ] `providers/adapters/splunk_adapter.py`
- [ ] `providers/adapters/mcp_adapter.py`

### Step 8 — Observability & Output
- [ ] `agent_trace.jsonl` — one entry per agent action (includes phase, action, detail)
- [ ] `a2a_messages.jsonl` — full message log with round numbers and status (completed/cancelled)
- [ ] `supervisor_state.json` — LangGraph state snapshot at each phase transition
- [ ] Console summary — human-readable RCA output including timed-out agents if any

### Step 9 — Open-Source Readiness
- [ ] `README.md` — Quickstart (clone → install → `python main.py --scenario 1` in under 5 min); all three replacement tiers documented with examples; architecture diagram; contributing guide
- [ ] All 10 scenarios run end-to-end with simulation (no external credentials)
- [ ] Scenario 10 confirmed: short-circuit fires after round 1, loop does not proceed to round 2
- [ ] Tier 1 replacement documented with working example config
- [ ] Tier 2 replacement documented with `ExternalAgentAdapterNode` usage example
- [ ] Clean install from `requirements.txt` produces working run
- [ ] Config constraint validation tested: violations raise `ConfigValidationError` at startup with clear message

---

## ⚠️ Architecture Decision Log

| Decision | Choice | Rationale |
|---|---|---|
| LangGraph ↔ ADK A2A integration | Agent-as-node wraps ADK A2A HTTP call; result returned to LangGraph state via reducer | Preserves LangGraph reducer and super-step guarantees. ADK A2A agents must not write results outside LangGraph state. |
| A2A wire format | JSON-RPC 2.0 over HTTP POST (`tasks/send`) | ADK A2A standard. Interoperable with third-party agents. `a2a_router.py` constructs all payloads. |
| SSE streaming | Not used. HTTP POST/response only | 2-round loop with 30s budget does not justify SSE complexity. `"streaming": false` declared in all Agent Cards. Revisit if async long-running agents are added. |
| A2A collaboration concurrency | Round-based collect-then-revise | Prevents stale-snapshot race condition. All agents revise once per round based on complete peer input. |
| A2A loop placement | LangGraph subgraph node | Termination enforced by LangGraph conditional edges, not application-level `while` loop. Preserves checkpointing across rounds. |
| Timeout architecture | Four distinct keys (`analysis_timeout_seconds`, `message_timeout_seconds`, `round_timeout_seconds`, `collaboration_timeout_seconds`) | Each boundary governs a different failure mode with different handling. Single `timeout_seconds` is ambiguous. Constraints validated at startup. |
| Agent replacement — Tier 1 | Change endpoint URL in config; `a2a_router.py` reads Agent Card and validates schema contract | Zero orchestrator changes for same-schema replacements. Contract enforced at registration. |
| Agent replacement — Tier 2 | `ExternalAgentAdapterNode` wraps external ADK A2A endpoint as LangGraph node | External agents don't need to know about LangGraph. Adapter node provides the LangGraph integration seam. |
| Agent replacement — Tier 3 | Schema extension via optional fields + version bump | Adding `default=None` fields is backward-compatible. Required field additions or type changes are breaking and require all agents to update simultaneously. |
| Confidence score computation | Computed deterministically by code after LLM synthesis, using formula + lookup tables | Non-deterministic LLM confidence scores are non-reproducible and untestable. Lookup tables produce auditable, consistent scores. |
| `timestamp_window` representation | Named fields `start_time` / `end_time` | Tuple serializes as JSON array — less readable in trace logs. |
| `target_agent` typing | `Union[Literal["broadcast"], str]` | Explicit type distinction prevents silent misrouting. |
| `incident_id` assignment | UUID generated at ingestion; `external_incident_id` preserved separately | Decouples NetCortex's internal key from source system ID. Both available in `IncidentRequest` and propagated to `RCAReport`. |
| Dependency pinning | All dependencies pinned from day one including `google-adk` | ADK API evolves rapidly. Unpinned = silent CI breakage. |
| Step 4 before Step 5 | A2A communication layer implemented before agents | Agents must implement against a defined interface. Building the interface after agents forces retroactive integration. |
| Built-in agent execution path | LangGraph nodes call built-in ADK A2A endpoints via self-HTTP | Maintains one protocol path for built-in and external agents, reducing implementation branching. |
| Incident injection | Separate helper script (`scripts/send_incident.py`) posts to webhook | Keeps ingestion testing reproducible across local runs and CI, with auth header testing support. |
| Webhook security | Deferred and optional for current open-source implementation | Keeps initial integration simple; adopters can add auth controls as needed. |
| Test process split | Unit vs integration/e2e run as separate processes | Keeps feedback fast locally while preserving deterministic integration coverage. |
| Analysis-phase peer handling | Queue peer messages during analysis; process only in collaboration phase | Eliminates race conditions between initial finding generation and peer-response handling. |
