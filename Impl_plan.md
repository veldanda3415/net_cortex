# NetCortex MCP 2026-07-28 Migration — Implementation Plan

**Version:** 2.0  
**Date:** 2026-08-06  
**Target:** v2.0 release by 2026-08-28  
**OAuth:** Deferred (not in this plan)  
**Deployment:** Local Kubernetes (single replica per service)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [What the 2026-07-28 Spec Actually Changed](#2-what-the-2026-07-28-spec-actually-changed)
3. [Current State of NetCortex](#3-current-state-of-netcortex)
4. [Implementation Scope](#4-implementation-scope)
5. [System Architecture (K8s)](#5-system-architecture-k8s)
6. [Component 1: MCP RCA Server](#6-component-1-mcp-rca-server)
7. [Component 2: MCP Telemetry Server](#7-component-2-mcp-telemetry-server)
8. [Component 3: MCP Provider Adapters (Client Side)](#8-component-3-mcp-provider-adapters-client-side)
9. [Kubernetes Deployment](#9-kubernetes-deployment)
10. [Config and Wiring Changes](#10-config-and-wiring-changes)
11. [File Layout (New/Modified)](#11-file-layout-newmodified)
12. [Execution Phases](#12-execution-phases)
13. [Testing Strategy](#13-testing-strategy)
14. [Risks and Mitigations](#14-risks-and-mitigations)
15. [Done-When Criteria](#15-done-when-criteria)

---

## 1. Executive Summary

This plan ports NetCortex to the MCP 2026-07-28 specification and deploys the result on local Kubernetes as a multi-service demo.

**Three services running on K8s:**

| Service | Role | Port |
|---------|------|------|
| **netcortex-rca** | MCP Server — exposes RCA tools to external clients | 9000 |
| **netcortex-telemetry** | MCP Server — serves simulation telemetry data via MCP tools | 9001 |
| **Domain agents** | Sidecar in RCA pod — A2A endpoints (internal) | 8001-8004 |

**Data flow:**
```
External MCP Client
    │
    ▼ (Streamable HTTP)
┌───────────────────────────┐          ┌──────────────────────────────┐
│ netcortex-rca (K8s Pod)   │          │ netcortex-telemetry (K8s Pod)│
│                           │   MCP    │                              │
│ MCP RCA Server :9000      │◄────────►│ MCP Telemetry Server :9001   │
│ ├── run_rca (Task)        │  client  │ ├── get_metrics             │
│ ├── get_report            │          │ ├── get_logs                │
│ └── list_scenarios        │          │ ├── get_routing_events      │
│                           │          │ └── get_config_changes      │
│ Domain Agents :8001-8004  │          │                              │
│ LangGraph Orchestrator    │          │ Serves SCENARIOS data        │
└───────────────────────────┘          └──────────────────────────────┘
```

The Python MCP SDK v2.0.0 (`mcp>=2.0.0`) supports the 2026-07-28 spec natively. We use `MCPServer` for both server surfaces and `Client` for the adapter calls between pods.

---

## 2. What the 2026-07-28 Spec Actually Changed

### Major Changes Relevant to Us

| Change | Impact on NetCortex |
|--------|-------------------|
| **Stateless core** — `initialize` handshake and `Mcp-Session-Id` removed | Both MCP servers have no sessions. Each request is self-contained. K8s can round-robin freely. |
| **`server/discover`** — servers MUST implement this RPC | Both servers respond to `server/discover`. SDK handles automatically. |
| **Per-request `_meta`** — protocol version + client capabilities in every request | SDK handles transparently. No manual work. |
| **Tasks extension (`io.modelcontextprotocol/tasks`)** — for long-running ops | `run_rca` returns a task handle; client polls via `tasks/get`. Perfect for RCA (30-60s). |
| **`tasks/get` replaces `tasks/result`** — polling model | Client drives lifecycle: `tasks/get`, `tasks/cancel`. No `tasks/list`. |
| **Streamable HTTP** — standard HTTP transport with SSE for streaming | Both servers use Streamable HTTP. Any MCP client can connect. K8s Ingress routes cleanly. |
| **`Mcp-Method` and `Mcp-Name` headers** — on every request | SDK handles. K8s Ingress/proxies can route without parsing JSON. |
| **`ttlMs` and `cacheScope`** on list results | `tools/list` includes cache hints. Telemetry server's tool list is highly cacheable. |
| **`resultType` field** — `"complete"` or `"task"` | SDK handles. Task results carry `resultType: "task"`. |
| **Roots, Sampling, Logging deprecated** | We don't use these. No impact. |
| **JSON Schema 2020-12** for tool schemas | Pydantic v2 generates compatible schemas. |

### What We Don't Need to Handle (deferred)

| Item | Reason |
|------|--------|
| OAuth/OIDC | Explicitly deferred |
| Multi Round-Trip Requests (MRTR) | Our tools don't need mid-call client input |
| `subscriptions/listen` | Polling via `tasks/get` is sufficient for MVP |
| MCP Apps | Not applicable to RCA |
| Dual-era compatibility | Modern-only; no legacy clients expected |
| Multi-replica task state (Redis) | Single replica per service for this demo |

---

## 3. Current State of NetCortex

### Architecture
```
CLI/Webhook → IncidentRequest → NetCortexEngine.run_incident()
    → Supervisor (classify + select agents)
    → Analysis (parallel domain agents via A2A)
    → Collaboration (A2A message exchange)
    → Synthesizer (aggregate findings → RCAReport)
    → Output artifacts (JSON files)
```

### Key Files
| File | Role |
|------|------|
| `app/main.py` | CLI entrypoint (Typer): `run`, `serve`, `eval` commands |
| `core/orchestrator.py` | `NetCortexEngine` — LangGraph workflow graph |
| `models/schemas.py` | Pydantic v2 models: `IncidentRequest`, `RCAReport`, `AgentFinding`, etc. |
| `providers/base.py` | Abstract provider interfaces (5 classes) |
| `providers/adapters/mcp_adapter.py` | 4 `NotImplementedError` stubs |
| `providers/simulation/` | Working simulation providers |
| `simulation/scenarios.py` | `SCENARIOS` dict with 14 bundled scenarios |
| `config/config.yaml` | Runtime config |

### Provider Interface Signatures
```python
class MetricsProvider(ABC):
    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None) -> list[MetricSnapshot]

class LogProvider(ABC):
    def get_logs(self, region: str, window_minutes: int, scenario_id: int | None) -> list[LogEvent]

class RoutingProvider(ABC):
    def get_routing_events(self, region: str, window_minutes: int, scenario_id: int | None) -> list[RoutingEvent]

class ConfigProvider(ABC):
    def get_config_changes(self, region: str, window_minutes: int, scenario_id: int | None) -> list[ConfigChange]
```

---

## 4. Implementation Scope

### In Scope
1. **MCP RCA Server** — 3 tools: `run_rca`, `get_report`, `list_scenarios` with Tasks extension
2. **MCP Telemetry Server** — 4 tools: `get_metrics`, `get_logs`, `get_routing_events`, `get_config_changes` serving simulation data via MCP
3. **MCP Provider Adapters** — 4 adapters consuming telemetry from the Telemetry Server over MCP
4. **Kubernetes deployment** — local K8s (minikube/kind/Docker Desktop), single replica per service
5. **Dockerfiles** — containerized builds for both services
6. **Config wiring** — provider factory, K8s service discovery
7. **Eval harness** — must remain green
8. **README v2.0 migration note**

### Out of Scope
- OAuth/OIDC (deferred)
- Real Prometheus/ELK/Splunk adapters
- Multi-replica + Redis task store
- New scenarios
- Web UI
- Cloud K8s (GKE/EKS/AKS)
- Distributed A2A without orchestrator mediation
- Helm charts (plain manifests are sufficient)

---

## 5. System Architecture (K8s)

### Cluster Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Local Kubernetes Cluster (minikube / kind / Docker Desktop K8s)         │
│                                                                          │
│  Namespace: netcortex                                                    │
│                                                                          │
│  ┌─────────────────────────────────┐  ┌──────────────────────────────┐  │
│  │ Deployment: netcortex-rca       │  │ Deployment: netcortex-telem  │  │
│  │ Replicas: 1                     │  │ Replicas: 1                  │  │
│  │                                 │  │                              │  │
│  │ Container: rca-server           │  │ Container: telemetry-server  │  │
│  │   MCP RCA Server :9000          │  │   MCP Telemetry Server :9001 │  │
│  │   Domain Agents :8001-8004      │  │   Serves SCENARIOS data      │  │
│  │   LangGraph Engine              │  │                              │  │
│  │   Task Store (in-memory)        │  │                              │  │
│  └────────────────┬────────────────┘  └──────────────┬───────────────┘  │
│                   │                                   │                   │
│  ┌────────────────▼────────────────┐  ┌──────────────▼───────────────┐  │
│  │ Service: netcortex-rca          │  │ Service: netcortex-telemetry │  │
│  │ ClusterIP :9000                 │  │ ClusterIP :9001              │  │
│  └─────────────────────────────────┘  └──────────────────────────────┘  │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ Service: netcortex-rca-external (NodePort or LoadBalancer)         │  │
│  │ External access → :9000 (for MCP Inspector / external clients)    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ ConfigMap: netcortex-config                                        │  │
│  │ (config.yaml with K8s service URLs for mcp_endpoints)             │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Service Discovery

Inside K8s, the RCA server connects to the telemetry server using K8s DNS:

```yaml
mcp_endpoints:
  metrics:
    url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
  logs:
    url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
  routing:
    url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
  config:
    url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
```

### Why K8s Proves the Spec

| Proof Point | How K8s Demonstrates It |
|-------------|------------------------|
| **Stateless** | Kill the RCA pod → K8s restarts it → next request works. No session lost. |
| **Service separation** | Telemetry and RCA are independent services. Replace telemetry with a real Prometheus-backed MCP server by changing one URL. |
| **Standard HTTP routing** | K8s Ingress/Services route MCP traffic like any HTTP. `Mcp-Method` headers visible in logs. |
| **Horizontal scaling** | (Future) scale RCA to 2+ replicas, swap in-memory TaskStore for Redis. Zero code change in MCP layer. |

---

## 6. Component 1: MCP RCA Server

### 6.1 Overview

The RCA server is the **external-facing** MCP server. Clients connect to it to run RCA analysis.

### 6.2 MCP Tools

#### Tool: `run_rca`

**Input Schema:**
```json
{
  "description": "string (required) — incident description",
  "region": "string (required) — affected region",
  "severity": "string (required) — low|medium|high|critical",
  "scenario_id": "integer (optional) — simulation scenario ID",
  "source_system": "string (optional) — originating system"
}
```

**Behavior:**
1. Validate input, create `IncidentRequest`
2. Create a Task (via Tasks extension)
3. Return task handle immediately (`task_id`, `status: "running"`)
4. In background: run `NetCortexEngine.run_incident()`
5. Push progress notifications at each pipeline stage:
   - `supervisor_complete` (25%)
   - `analysis_complete` (50%)
   - `collaboration_complete` (75%)
   - `synthesis_complete` (100%)
6. On completion: store result, mark task `completed`
7. On failure: mark task `failed` with error details

**Critical invariants in task result:**
- `conflict_detected` field MUST be present
- Timed-out agents appear with `timed_out_agents: [...]`, NOT as failures
- `confidence_score` passes through unchanged — MCP layer is pure transport

#### Tool: `get_report`

**Input:** `{ "incident_id": "string" }`  
**Behavior:** Look up completed RCA report. Return full `RCAReport` JSON or error if not found/still running.

#### Tool: `list_scenarios`

**Input:** `{}`  
**Behavior:** Return list of available simulation scenarios (`id`, `name`, `description`).

### 6.3 Task Lifecycle

```
Client → tools/call("run_rca", {...})
    ← resultType="task", taskId="task_abc123", status="running"

Client → tasks/get("task_abc123")
    ← status="running", progress={ stage: "analysis", percent: 50 }

Client → tasks/get("task_abc123")
    ← status="completed", result={ ...full RCAReport... }
```

**Task states:** `running` → `completed` | `failed` | `cancelled`

### 6.4 Task Store (In-Memory, Single Replica)

```python
class TaskStore:
    """In-memory task state for RCA runs. Single-replica only."""
    _tasks: dict[str, TaskRecord]

    def create_task(self, incident_id: str) -> str
    def update_progress(self, task_id: str, stage: str, percent: float)
    def complete_task(self, task_id: str, result: RCAReport)
    def fail_task(self, task_id: str, error: str)
    def get_task(self, task_id: str) -> TaskRecord | None
```

### 6.5 Server Startup

```python
@app.command(name="mcp-serve")
def mcp_serve(
    host: str = "0.0.0.0",
    port: int = 9000,
    config: str = "config/config.yaml",
):
    """Start NetCortex as an MCP server (2026-07-28 spec)."""
    # 1. Load config
    # 2. Start domain agent services (8001-8004) as background tasks
    # 3. Initialize NetCortexEngine (with MCP adapter providers)
    # 4. Create MCPServer with tools registered
    # 5. Serve over Streamable HTTP on :9000
```

---

## 7. Component 2: MCP Telemetry Server

### 7.1 Overview

A **new, standalone MCP server** that wraps the existing simulation data (`SCENARIOS` dict) and serves it via standard MCP tools. This is what the provider adapters in the RCA pod connect to.

In production, this would be replaced by real Prometheus/ELK/Splunk-backed MCP servers. For the demo, it serves the same simulation data that `SimulationMetricsProvider` etc. use today — but accessed over the MCP protocol.

### 7.2 MCP Tools

#### Tool: `get_metrics`

**Input:** `{ "region": "string", "window_minutes": "int", "scenario_id": "int (optional, default 1)" }`  
**Output:**
```json
{
  "metrics": [
    {
      "timestamp": "2026-05-05T10:14:00Z",
      "region": "us-east",
      "error_rate": 0.081,
      "packet_loss": 0.024,
      "throughput_gbps": 0.61,
      "latency_ms": 124.5,
      "tags": {"service": "gateway", "node": "edge-3"}
    }
  ]
}
```

#### Tool: `get_logs`

**Input:** `{ "region": "string", "window_minutes": "int", "scenario_id": "int (optional)" }`  
**Output:**
```json
{
  "logs": [
    {
      "timestamp": "2026-05-05T10:14:00Z",
      "level": "ERROR",
      "service": "gateway",
      "message": "Connection timeout to upstream"
    }
  ]
}
```

#### Tool: `get_routing_events`

**Input:** `{ "region": "string", "window_minutes": "int", "scenario_id": "int (optional)" }`  
**Output:**
```json
{
  "routing_events": [
    {
      "timestamp": "2026-05-05T10:10:00Z",
      "region": "us-east",
      "path_id": "path-42",
      "change_type": "reroute",
      "details": "BGP path changed from AS100 to AS200"
    }
  ]
}
```

#### Tool: `get_config_changes`

**Input:** `{ "region": "string", "window_minutes": "int", "scenario_id": "int (optional)" }`  
**Output:**
```json
{
  "config_changes": [
    {
      "timestamp": "2026-05-05T09:55:00Z",
      "component": "Switch-C eth0/1",
      "change_type": "bandwidth_limit",
      "before": {"bandwidth": "10Gbps"},
      "after": {"bandwidth": "1Gbps"}
    }
  ]
}
```

### 7.3 Implementation

```python
# mcp_telemetry_server/server.py
from mcp.server import MCPServer
from simulation.scenarios import SCENARIOS
from models.schemas import MetricSnapshot

telemetry = MCPServer("NetCortex-Telemetry")

@telemetry.tool()
def get_metrics(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch network metrics telemetry for a region and time window."""
    scenario = SCENARIOS.get(scenario_id, SCENARIOS[1])
    metrics = [m.model_dump(mode="json") for m in scenario.metrics_data if m.region == region]
    return {"metrics": metrics}

@telemetry.tool()
def get_logs(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch log events for a region and time window."""
    scenario = SCENARIOS.get(scenario_id, SCENARIOS[1])
    logs = [l.model_dump(mode="json") for l in scenario.log_events]
    return {"logs": logs}

@telemetry.tool()
def get_routing_events(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch routing change events for a region and time window."""
    scenario = SCENARIOS.get(scenario_id, SCENARIOS[1])
    events = [r.model_dump(mode="json") for r in scenario.routing_events if r.region == region]
    return {"routing_events": events}

@telemetry.tool()
def get_config_changes(region: str, window_minutes: int, scenario_id: int = 1) -> dict:
    """Fetch configuration changes for a region and time window."""
    scenario = SCENARIOS.get(scenario_id, SCENARIOS[1])
    changes = [c.model_dump(mode="json") for c in scenario.config_changes]
    return {"config_changes": changes}
```

### 7.4 Entrypoint

```python
# mcp_telemetry_server/__main__.py
"""Run the MCP Telemetry Server standalone (Streamable HTTP on port 9001)."""
from mcp_telemetry_server.server import telemetry

if __name__ == "__main__":
    telemetry.run(transport="streamable-http", host="0.0.0.0", port=9001)
```

### 7.5 Why Separate Service?

- **Proves MCP transport end-to-end** — adapters call over real HTTP, not in-memory mocks
- **K8s service discovery** — RCA pod discovers telemetry pod via `netcortex-telemetry:9001`
- **Replaceable** — swap for real backends by deploying a different MCP server at same service name
- **Independent lifecycle** — telemetry server can restart without affecting in-flight RCA tasks

---

## 8. Component 3: MCP Provider Adapters (Client Side)

### 8.1 Overview

The RCA pod's domain agents use **MCP provider adapters** to fetch telemetry from the telemetry pod. The adapters are MCP clients connecting to `http://netcortex-telemetry:9001/mcp`.

### 8.2 Implementation Pattern

```python
import asyncio
import logging
from datetime import datetime
from mcp import Client
from providers.base import MetricsProvider
from models.schemas import MetricSnapshot

logger = logging.getLogger("net_cortex.adapter.metrics")

class MCPMetricsAdapter(MetricsProvider):
    def __init__(self, endpoint: str, timeout: int = 30):
        self._endpoint = endpoint
        self._timeout = timeout

    def get_metrics(self, region: str, window_minutes: int, scenario_id: int | None) -> list[MetricSnapshot]:
        """Fetch metrics from MCP Telemetry Server."""
        try:
            return asyncio.run(self._fetch(region, window_minutes, scenario_id))
        except Exception as e:
            logger.warning("MCP metrics fetch failed endpoint=%s error=%s", self._endpoint, e)
            return []

    async def _fetch(self, region: str, window_minutes: int, scenario_id: int | None) -> list[MetricSnapshot]:
        async with Client(self._endpoint) as client:
            result = await client.call_tool("get_metrics", {
                "region": region,
                "window_minutes": window_minutes,
                "scenario_id": scenario_id or 1,
            })
            return self._parse(result)

    def _parse(self, result) -> list[MetricSnapshot]:
        snapshots = []
        for row in result.structured_content.get("metrics", []):
            try:
                snapshots.append(MetricSnapshot(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    region=row["region"],
                    error_rate=float(row["error_rate"]),
                    packet_loss=float(row["packet_loss"]),
                    throughput_gbps=float(row["throughput_gbps"]),
                    latency_ms=float(row["latency_ms"]),
                    tags=row.get("tags", {}),
                ))
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed metric row: %s", e)
        return snapshots
```

### 8.3 All Four Adapters

| Adapter | Calls Tool | Returns | Parses |
|---------|-----------|---------|--------|
| `MCPMetricsAdapter` | `get_metrics` | `list[MetricSnapshot]` | timestamp, region, error_rate, packet_loss, throughput_gbps, latency_ms, tags |
| `MCPLogAdapter` | `get_logs` | `list[LogEvent]` | timestamp, level, service, message |
| `MCPRoutingAdapter` | `get_routing_events` | `list[RoutingEvent]` | timestamp, region, path_id, change_type, details |
| `MCPConfigAdapter` | `get_config_changes` | `list[ConfigChange]` | timestamp, component, change_type, before, after |

### 8.4 Failure Handling

- **Timeout:** Return `[]`, log warning. Agent proceeds with no data.
- **Server unreachable:** Return `[]`. Don't crash.
- **Schema mismatch:** Skip malformed rows, return valid ones.
- **Partial data:** Return what's available. Agent scores confidence lower.

### 8.5 Provider Factory

```python
# providers/factory.py
from providers.base import MetricsProvider, LogProvider, RoutingProvider, ConfigProvider
from providers.simulation.metrics_sim import SimulationMetricsProvider
from providers.simulation.log_sim import SimulationLogProvider
from providers.simulation.routing_sim import SimulationRoutingProvider
from providers.simulation.config_sim import SimulationConfigProvider
from providers.adapters.mcp_adapter import (
    MCPMetricsAdapter, MCPLogAdapter, MCPRoutingAdapter, MCPConfigAdapter
)

def create_metrics_provider(cfg: dict) -> MetricsProvider:
    mode = cfg["providers"]["metrics"]
    if mode == "simulation":
        return SimulationMetricsProvider()
    elif mode == "mcp":
        ep = cfg["mcp_endpoints"]["metrics"]
        return MCPMetricsAdapter(endpoint=ep["url"], timeout=ep["timeout_seconds"])
    raise ValueError(f"Unknown metrics provider mode: {mode}")

# Same pattern for logs, routing, config...
```

---

## 9. Kubernetes Deployment

### 9.1 Manifests

```
k8s/
├── namespace.yaml
├── configmap.yaml
├── telemetry-deployment.yaml
├── telemetry-service.yaml
├── rca-deployment.yaml
├── rca-service.yaml
└── rca-nodeport.yaml          # external access for demos
```

### 9.2 Namespace

```yaml
# k8s/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: netcortex
```

### 9.3 ConfigMap

```yaml
# k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: netcortex-config
  namespace: netcortex
data:
  config.yaml: |
    llm:
      provider: google
      model: gemini-2.5-flash
      api_key_env: GEMINI_API_KEY
      require_success: false

    providers:
      metrics: mcp
      logs: mcp
      routing: mcp
      config: mcp

    mcp_endpoints:
      metrics:
        url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
        timeout_seconds: 30
      logs:
        url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
        timeout_seconds: 30
      routing:
        url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
        timeout_seconds: 30
      config:
        url: "http://netcortex-telemetry.netcortex.svc.cluster.local:9001/mcp"
        timeout_seconds: 30

    mcp_server:
      host: "0.0.0.0"
      port: 9000
      transport: streamable_http
      server_name: "NetCortex-RCA"
      server_version: "2.0.0"
      task_ttl_seconds: 3600
      max_concurrent_tasks: 10

    a2a:
      protocol_mode: adk
      max_iterations: 2
      analysis_timeout_seconds: 20
      message_timeout_seconds: 10
      round_timeout_seconds: 25
      collaboration_timeout_seconds: 60

    simulation:
      region: us-east
      window_minutes: 30

    baselines:
      provider: simulation
      metrics_z_threshold: 3.0
      config_z_threshold: 2.5
      legacy_fallback: true

    agents:
      metrics:
        endpoint: http://localhost:8001/a2a
        card_url: http://localhost:8001/.well-known/agent.json
        use_external_adapter: false
      log:
        endpoint: http://localhost:8002/a2a
        card_url: http://localhost:8002/.well-known/agent.json
        use_external_adapter: false
      routing:
        endpoint: http://localhost:8003/a2a
        card_url: http://localhost:8003/.well-known/agent.json
        use_external_adapter: false
      config:
        endpoint: http://localhost:8004/a2a
        card_url: http://localhost:8004/.well-known/agent.json
        use_external_adapter: false

    ingestion:
      host: 0.0.0.0
      port: 8000
```

### 9.4 Telemetry Server Deployment

```yaml
# k8s/telemetry-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: netcortex-telemetry
  namespace: netcortex
spec:
  replicas: 1
  selector:
    matchLabels:
      app: netcortex-telemetry
  template:
    metadata:
      labels:
        app: netcortex-telemetry
    spec:
      containers:
      - name: telemetry
        image: netcortex-telemetry:latest
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 9001
        readinessProbe:
          httpGet:
            path: /mcp
            port: 9001
          initialDelaySeconds: 5
          periodSeconds: 10
```

### 9.5 RCA Server Deployment

```yaml
# k8s/rca-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: netcortex-rca
  namespace: netcortex
spec:
  replicas: 1
  selector:
    matchLabels:
      app: netcortex-rca
  template:
    metadata:
      labels:
        app: netcortex-rca
    spec:
      containers:
      - name: rca-server
        image: netcortex-rca:latest
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 9000
        volumeMounts:
        - name: config
          mountPath: /app/config/config.yaml
          subPath: config.yaml
        env:
        - name: GEMINI_API_KEY
          valueFrom:
            secretKeyRef:
              name: netcortex-secrets
              key: gemini-api-key
              optional: true
        readinessProbe:
          httpGet:
            path: /mcp
            port: 9000
          initialDelaySeconds: 10
          periodSeconds: 10
      volumes:
      - name: config
        configMap:
          name: netcortex-config
```

### 9.6 Services

```yaml
# k8s/telemetry-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: netcortex-telemetry
  namespace: netcortex
spec:
  selector:
    app: netcortex-telemetry
  ports:
  - port: 9001
    targetPort: 9001
---
# k8s/rca-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: netcortex-rca
  namespace: netcortex
spec:
  selector:
    app: netcortex-rca
  ports:
  - port: 9000
    targetPort: 9000
---
# k8s/rca-nodeport.yaml (external access for demo)
apiVersion: v1
kind: Service
metadata:
  name: netcortex-rca-external
  namespace: netcortex
spec:
  type: NodePort
  selector:
    app: netcortex-rca
  ports:
  - port: 9000
    targetPort: 9000
    nodePort: 30900
```

### 9.7 Dockerfiles

```dockerfile
# Dockerfile.rca
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 9000 8001 8002 8003 8004
CMD ["python", "app/main.py", "mcp-serve"]
```

```dockerfile
# Dockerfile.telemetry
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 9001
CMD ["python", "-m", "mcp_telemetry_server"]
```

### 9.8 Demo Script

```powershell
# deploy.ps1 — one-shot demo deployment
# Prerequisites: minikube running, kubectl configured

# Build images (using minikube's Docker daemon)
minikube docker-env | Invoke-Expression
docker build -t netcortex-rca:latest -f Dockerfile.rca .
docker build -t netcortex-telemetry:latest -f Dockerfile.telemetry .

# Deploy
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/telemetry-deployment.yaml
kubectl apply -f k8s/telemetry-service.yaml
kubectl apply -f k8s/rca-deployment.yaml
kubectl apply -f k8s/rca-service.yaml
kubectl apply -f k8s/rca-nodeport.yaml

# Wait for readiness
kubectl -n netcortex wait --for=condition=ready pod -l app=netcortex-telemetry --timeout=60s
kubectl -n netcortex wait --for=condition=ready pod -l app=netcortex-rca --timeout=90s

# Get external URL
$RCA_URL = "http://$(minikube ip):30900/mcp"
Write-Host "NetCortex RCA MCP Server available at: $RCA_URL"
Write-Host "Test with: npx @modelcontextprotocol/inspector"
```

---

## 10. Config and Wiring Changes

### 10.1 config.yaml (K8s version — served via ConfigMap)

Key difference from local config: `providers` set to `mcp` and `mcp_endpoints` point to K8s service DNS.

### 10.2 config.yaml (Local development — for eval harness)

Stays unchanged with `providers: simulation`. Eval harness always runs against simulation directly — no MCP network hop — to keep it fast and deterministic.

### 10.3 Provider Factory

```python
# providers/factory.py
def create_metrics_provider(cfg: dict) -> MetricsProvider:
    mode = cfg["providers"]["metrics"]
    if mode == "simulation":
        return SimulationMetricsProvider()
    elif mode == "mcp":
        ep = cfg["mcp_endpoints"]["metrics"]
        return MCPMetricsAdapter(endpoint=ep["url"], timeout=ep["timeout_seconds"])
    raise ValueError(f"Unknown provider: {mode}")
```

---

## 11. File Layout (New/Modified)

### New Files

```
net_cortex/
├── mcp_server/
│   ├── __init__.py
│   ├── server.py              # MCPServer setup, tool registration for RCA
│   ├── tools.py               # run_rca, get_report, list_scenarios handlers
│   └── tasks.py               # TaskStore, TaskRecord, task lifecycle
├── mcp_telemetry_server/
│   ├── __init__.py
│   ├── server.py              # MCPServer setup, 4 telemetry tools
│   └── __main__.py            # Standalone entrypoint (port 9001)
├── providers/
│   └── factory.py             # Provider factory (create by mode)
├── k8s/
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── telemetry-deployment.yaml
│   ├── telemetry-service.yaml
│   ├── rca-deployment.yaml
│   ├── rca-service.yaml
│   └── rca-nodeport.yaml
├── Dockerfile.rca
├── Dockerfile.telemetry
└── deploy.ps1                 # One-shot K8s deployment script
```

### Modified Files

| File | Changes |
|------|---------|
| `app/main.py` | Add `mcp-serve` command |
| `config/config.yaml` | Add `mcp_server` section, expand `mcp_endpoints` (local stays simulation) |
| `requirements.txt` | Add `mcp>=2.0.0` |
| `providers/adapters/mcp_adapter.py` | Replace stubs with full implementations |
| `README.md` | Add MCP server section, K8s deployment, migration note |

### Unchanged Files (Critical — Must Not Touch)

| File | Reason |
|------|--------|
| `core/orchestrator.py` | Engine logic stays the same |
| `models/schemas.py` | Data models are unchanged |
| `providers/base.py` | Interfaces stay the same |
| `simulation/scenarios.py` | Scenario data is unchanged |
| `agents/*.py` | Agent logic is unchanged |

---

## 12. Execution Phases

### Phase 0: Pre-Migration Baseline (Day 1)
- [ ] Run `python app/main.py eval --all-scenarios --fail-on-miss`
- [ ] Capture output as `output/baseline_eval_pre_migration.txt`
- [ ] Verify scenarios 10, 12 conflict detection by hand
- [ ] Commit baseline

### Phase 1: MCP Telemetry Server (Days 2-4)
- [ ] Create `mcp_telemetry_server/` module
- [ ] Implement 4 tools: `get_metrics`, `get_logs`, `get_routing_events`, `get_config_changes`
- [ ] Add `__main__.py` entrypoint
- [ ] Update `requirements.txt` with `mcp>=2.0.0`
- [ ] Verify with MCP Inspector: tools listed, tools callable, simulation data returned
- [ ] Run standalone: `python -m mcp_telemetry_server`

### Phase 2: MCP RCA Server (Days 4-7)
- [ ] Create `mcp_server/` module structure
- [ ] Implement `TaskStore` (in-memory)
- [ ] Implement `list_scenarios` tool (simplest, test SDK wiring)
- [ ] Implement `get_report` tool
- [ ] Implement `run_rca` tool with Task lifecycle + progress updates
- [ ] Wire into `app/main.py` as `mcp-serve` command
- [ ] Verify with MCP Inspector end-to-end: call `run_rca`, poll task, get report

### Phase 3: MCP Provider Adapters + Factory (Days 7-9)
- [ ] Create `providers/factory.py`
- [ ] Implement all 4 adapters in `mcp_adapter.py` (replace stubs)
- [ ] Wire factory into agent startup path
- [ ] Test locally: start telemetry server on 9001, start RCA server on 9000 with `providers: mcp`
- [ ] Verify end-to-end: MCP client → RCA server → adapters → telemetry server → response

### Phase 4: Containerize + K8s Deploy (Days 9-12)
- [ ] Create `Dockerfile.rca` and `Dockerfile.telemetry`
- [ ] Build and test images locally with `docker run`
- [ ] Create all K8s manifests (`k8s/`)
- [ ] Write `deploy.ps1` script
- [ ] Deploy to local K8s
- [ ] Verify: `kubectl -n netcortex get pods` — both running
- [ ] Verify external access: MCP Inspector connects to `http://$(minikube ip):30900/mcp`
- [ ] Run full demo: trigger `run_rca`, poll task, retrieve report

### Phase 5: Regression + Polish (Days 12-14)
- [ ] Run eval harness (local, simulation mode): `python app/main.py eval --all-scenarios --fail-on-miss`
- [ ] Compare with baseline — must be identical
- [ ] Verify `conflict_detected` in Task result (scenarios 10, 12)
- [ ] Verify timed-out agents in Task result
- [ ] Verify confidence scores unchanged
- [ ] Update README.md with full migration note + K8s deployment instructions
- [ ] Tag `v2.0`

---

## 13. Testing Strategy

### 13.1 Eval Harness (Non-Negotiable)

```powershell
python app/main.py eval --all-scenarios --fail-on-miss
```

Runs locally with `providers: simulation` (no MCP hop). Must produce **identical** keyword coverage to pre-migration baseline.

### 13.2 MCP Telemetry Server Tests

```python
async def test_get_metrics_returns_scenario_data():
    """get_metrics returns MetricSnapshot-compatible data for scenario 1."""

async def test_get_metrics_filters_by_region():
    """Only metrics matching the requested region are returned."""

async def test_unknown_scenario_falls_back_to_1():
    """Invalid scenario_id returns scenario 1 data."""

async def test_all_tools_listed():
    """tools/list returns 4 telemetry tools."""
```

### 13.3 MCP RCA Server Tests

```python
async def test_list_scenarios():
    """list_scenarios returns all 14 bundled scenarios."""

async def test_run_rca_creates_task():
    """run_rca returns a task handle immediately."""

async def test_task_completes_with_report():
    """Task transitions: running → completed with full RCAReport."""

async def test_conflict_detected_in_task_result():
    """Scenarios 10/12: conflict_detected=True in task result."""

async def test_timed_out_agents_preserved():
    """Timed-out agents appear as timeout, not failure."""
```

### 13.4 MCP Adapter Tests

```python
async def test_adapter_parses_response():
    """MCPMetricsAdapter converts tool response to list[MetricSnapshot]."""

async def test_adapter_timeout_returns_empty():
    """Returns [] on timeout, no crash."""

async def test_adapter_unreachable_returns_empty():
    """Returns [] when telemetry server is down."""

async def test_adapter_skips_malformed_rows():
    """Valid rows returned, bad rows skipped with warning."""
```

### 13.5 K8s Integration Test

```powershell
# After deploy.ps1
$RCA_URL = "http://$(minikube ip):30900/mcp"

# Verify server/discover
curl -X POST $RCA_URL -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"server/discover"}'

# Verify tools/list
curl -X POST $RCA_URL -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# Trigger RCA and poll (or use MCP Inspector)
npx @modelcontextprotocol/inspector
```

---

## 14. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| MCP SDK v2 API different from docs | Medium | High | Pin exact version, test telemetry server first (Phase 1) |
| Tasks extension not fully baked in SDK | Medium | High | Use `fastmcp-tasks` package OR manual task handling |
| Async/sync boundary in adapters | Medium | Medium | Test patterns early; fallback to `asyncio.run()` |
| K8s pod startup order (RCA before telemetry) | Medium | Low | Readiness probes + adapter retry on first call |
| Docker image size (Python + deps) | Low | Low | Multi-stage build with slim base |
| Eval regression from provider factory | Low | High | Factory default is `simulation`; eval config untouched |
| `conflict_detected` lost in Task serialization | Low | Critical | Explicit test; manual check of scenarios 10, 12 |
| Minikube networking issues | Low | Medium | Test `docker run` locally first before K8s deploy |

---

## 15. Done-When Criteria

### Functional
- [ ] `python app/main.py eval --all-scenarios --fail-on-miss` passes (identical to baseline)
- [ ] MCP Telemetry Server serves simulation data via 4 MCP tools
- [ ] MCP RCA Server exposes `run_rca`, `get_report`, `list_scenarios`
- [ ] `run_rca` creates a Task, runs the full pipeline, returns completed report
- [ ] MCP adapters fetch data from Telemetry Server over MCP protocol
- [ ] Scenarios 10, 12 produce `conflict_detected: true` in Task result
- [ ] Timed-out agents preserved as timeout, not failure
- [ ] Confidence scores byte-for-byte identical to pre-migration

### Deployment
- [ ] Both services containerized and buildable: `docker build`
- [ ] K8s manifests deploy both pods to local cluster: `kubectl apply -f k8s/`
- [ ] Pods reach Ready state within 90 seconds
- [ ] External MCP client (Inspector) can connect to RCA server via NodePort
- [ ] Full demo works: trigger RCA → poll task → get report

### Documentation
- [ ] `requirements.txt` includes `mcp>=2.0.0`
- [ ] README.md has v2.0 migration note, MCP server usage, K8s deployment
- [ ] Repo tagged `v2.0`

---

## Appendix A: SDK Quick Reference (Python MCP v2.0.0)

```python
# Server
from mcp.server import MCPServer
mcp = MCPServer("name")

@mcp.tool()
async def my_tool(arg: str) -> dict:
    """Tool description."""
    return {"result": "..."}

# Client
from mcp import Client
async with Client("http://server:port/mcp") as client:
    result = await client.call_tool("tool_name", {"arg": "value"})
    print(result.structured_content)

# Run server (Streamable HTTP)
# CLI: mcp run server.py --transport streamable-http --port 9000
# Or: server.run(transport="streamable-http", host="0.0.0.0", port=9000)
```

## Appendix B: MCP 2026-07-28 Spec Sources

- Official changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Python SDK v2: https://github.com/modelcontextprotocol/python-sdk (v2.0.0)
- Tasks extension: `io.modelcontextprotocol/tasks` (polling via `tasks/get`, no `tasks/list`)
- fastmcp-tasks package: https://pypi.org/project/fastmcp-tasks
- Migration guide (community): https://www.mcpjam.com/blog/mcp-v2-2026-07-28-migration-guide

Content in this appendix was rephrased for compliance with licensing restrictions.

## Appendix C: Local Development (No K8s)

For rapid iteration before containerizing:

```powershell
# Terminal 1: Start Telemetry Server
python -m mcp_telemetry_server

# Terminal 2: Start RCA Server (providers: mcp, pointing to localhost:9001)
python app/main.py mcp-serve --config config/config_mcp.yaml

# Terminal 3: MCP Inspector
npx @modelcontextprotocol/inspector
```

`config/config_mcp.yaml` is a local variant with:
```yaml
providers:
  metrics: mcp
  logs: mcp
  routing: mcp
  config: mcp

mcp_endpoints:
  metrics:
    url: "http://localhost:9001/mcp"
    timeout_seconds: 30
  # ... same for others
```
