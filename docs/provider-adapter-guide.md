# Provider Adapter Guide

This guide explains how to connect real telemetry backends to NetCortex without changing orchestration logic.

## Adapter Principle

NetCortex separates decision logic from data retrieval.

- Adapters fetch raw data from external systems.
- Agents consume normalized provider outputs.
- Orchestrator remains unchanged.

## Current Extension Points

Implement these files:

- `providers/adapters/prometheus_adapter.py`
- `providers/adapters/elk_adapter.py`
- `providers/adapters/splunk_adapter.py`
- `providers/adapters/mcp_adapter.py`

Each adapter should conform to the abstract provider interface for its domain.

## Adapter Design Requirements

1. Deterministic return shape
- Return only schema-compatible objects.
- Convert backend-specific fields to canonical model fields.

2. Time-window fidelity
- Respect requested region and window exactly.
- Avoid hidden default windows.

3. Reliability controls
- Request timeout
- Retry with bounded attempts
- Circuit-breaker or fast-fail mode when upstream is down

4. Data quality metadata
- Return enough context for agent confidence down-weighting when data is stale/incomplete.

5. Security
- Never log secrets or bearer tokens.
- Use environment variables or secret managers for credentials.

## Minimal Adapter Contract by Domain

### Metrics Adapter
Must provide:
- timestamp
- region
- error_rate
- packet_loss
- throughput_gbps
- latency_ms

Example normalized row:

```json
{
	"timestamp": "2026-05-05T10:14:00Z",
	"region": "us-east",
	"error_rate": 0.081,
	"packet_loss": 0.024,
	"throughput_gbps": 0.61,
	"latency_ms": 124.5,
	"tags": {
		"service": "gateway",
		"node": "edge-3"
	}
}
```

### Log Adapter
Must provide:
- timestamp
- level
- service
- message

### Routing Adapter
Must provide:
- timestamp
- region
- path_id
- change_type
- details

### Config Adapter
Must provide:
- timestamp
- component
- change_type
- before
- after

## Backend Mapping Notes

### Prometheus
- Map queries to normalized metric snapshots.
- Use recording rules when raw queries are too expensive.

### ELK/Splunk
- Normalize severity and service fields.
- Keep message truncation deterministic.

### MCP
- Keep MCP tool contracts stable.
- Validate response payload shape before model conversion.

## Configuration Pattern

Use `config/config.yaml` to choose provider mode by domain.

Example strategy:

- Start hybrid: metrics real, others simulation.
- Validate output quality.
- Progressively move remaining domains to real adapters.

## Adapter Validation Checklist

1. Unit tests for field mapping.
2. Timeout and retry behavior tests.
3. Empty-result behavior test.
4. Partial-data behavior test.
5. End-to-end scenario run with adapter enabled.

## Rollout Strategy

1. Shadow mode
- Run adapter in parallel to simulation, compare findings.

2. Canary mode
- Enable adapter for subset of incidents/regions.

3. Full mode
- Switch provider in config after stability and quality thresholds are met.
