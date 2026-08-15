"""Shared fixtures for MCP server / adapter integration tests.

These fixtures build the *real* domain agents (agents/metrics_agent.py etc.)
as in-process ASGI apps and drive them through a minimal test-only router
that speaks the same JSON-RPC envelope as communication/a2a_router.py, but
dispatches via httpx's ASGI transport instead of opening real sockets.

Why not just monkeypatch NetCortexEngine.run_incident() with a canned
RCAReport? Because the thing we actually need to verify — confidence
scoring, conflict_detected, timed_out_agents propagation through
mcp_server/server.py's Task result — depends on the *real* synthesizer and
*real* collaboration loop, not a stub. Faking the engine would test the MCP
plumbing against data shapes we invented ourselves, which is exactly the
kind of test that stays green while the real integration breaks.

tool_fn / poll_until_done are exposed as fixtures (not module-level
functions imported via `from tests.integration.X import Y`) deliberately:
this repo has no tests/__init__.py or tests/integration/__init__.py, so a
dotted cross-module import depends on pytest's rootdir-insertion behavior
and can silently break in CI depending on how pytest is invoked. Fixtures
defined in conftest.py are auto-discovered by pytest for every test file in
this directory tree with no import required — the portable pattern.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from agents.config_agent import build_config_app
from agents.log_agent import build_log_app
from agents.metrics_agent import build_metrics_app
from agents.routing_agent import build_routing_app
from communication.message_types import JsonRpcMessage, JsonRpcPart, TaskParams, TaskRequest
from core.orchestrator import NetCortexEngine
from models.schemas import A2AMessage, AgentFinding


BASE_CFG: dict[str, Any] = {
    "llm": {"provider": "google", "model": "gemini-2.5-flash", "require_success": False},
    "providers": {"metrics": "simulation", "logs": "simulation", "routing": "simulation", "config": "simulation"},
    "a2a": {
        "protocol_mode": "custom",
        "max_iterations": 2,
        "analysis_timeout_seconds": 20,
        "message_timeout_seconds": 10,
        "round_timeout_seconds": 25,
        "collaboration_timeout_seconds": 60,
    },
    "simulation": {"region": "us-east", "window_minutes": 30},
    "baselines": {
        "provider": "simulation",
        "metrics_z_threshold": 3.0,
        "config_z_threshold": 2.5,
        "legacy_fallback": True,
    },
    "agents": {
        "metrics": {"endpoint": "http://metrics-agent/a2a"},
        "log": {"endpoint": "http://log-agent/a2a"},
        "routing": {"endpoint": "http://routing-agent/a2a"},
        "config": {"endpoint": "http://config-agent/a2a"},
    },
    "mcp_server": {"task_ttl_seconds": 3600, "max_concurrent_tasks": 10, "auth": {"enabled": False}},
}


@pytest.fixture
def cfg() -> dict[str, Any]:
    return copy.deepcopy(BASE_CFG)


class ASGITestRouter:
    """RouterBase implementation that dispatches to in-process ASGI agent apps.

    Mirrors communication/a2a_router.py's JSON-RPC envelope exactly, so a
    passing test here is evidence about the real wire format, not a
    reimplementation of it.
    """

    def __init__(self, clients: dict[str, httpx.AsyncClient]) -> None:
        self._clients = clients  # agent_id -> AsyncClient bound to that agent's ASGI app

    async def _post(self, agent_id: str, payload: dict) -> dict:
        client = self._clients[agent_id]
        resp = await client.post("/a2a", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def send_analysis(self, agent_id: str, incident_id: str, skill: str, payload_data: dict) -> AgentFinding:
        req = TaskRequest(
            id=f"req-{uuid4()}",
            params=TaskParams(
                id=f"task-{agent_id}-{incident_id}",
                sessionId=incident_id,
                message=JsonRpcMessage(parts=[
                    JsonRpcPart(type="text", text=f"Run {skill}"),
                    JsonRpcPart(type="data", data={"skill": skill, **payload_data}),
                ]),
            ),
        )
        resp = await self._post(agent_id, req.model_dump())
        artifact_data = resp["result"]["artifacts"][0]["parts"][0]["data"]
        return AgentFinding.model_validate(artifact_data)

    async def send_direct(self, sender: str, target: str, message_type: str, payload: dict,
                           round_number: int, session_id: str) -> A2AMessage:
        req = TaskRequest(
            id=f"req-{uuid4()}",
            params=TaskParams(
                id=f"task-a2a-{sender}-to-{target}-r{round_number}",
                sessionId=session_id,
                message=JsonRpcMessage(parts=[
                    JsonRpcPart(type="text", text=message_type),
                    JsonRpcPart(type="data", data={
                        "skill": "respond-to-peer",
                        "message_type": message_type,
                        "sender_agent": sender,
                        "round_number": round_number,
                        "payload": payload,
                    }),
                ]),
            ),
        )
        try:
            resp = await self._post(target, req.model_dump())
            state = str(resp.get("result", {}).get("status", {}).get("state", "completed")).lower()
            status = "completed" if state in {"completed", "queued", "working", "submitted"} else "failed"
        except Exception:
            status = "failed"
        return A2AMessage(
            sender_agent=sender, target_agent=target, message_type=message_type,
            payload={"status": status, **payload}, round_number=round_number,
            timestamp=datetime.now(timezone.utc),
        )

    async def broadcast(self, sender: str, message_type: str, payload: dict,
                         round_number: int, session_id: str) -> list[A2AMessage]:
        results = []
        for target in self._clients:
            if target == sender:
                continue
            results.append(await self.send_direct(sender, target, message_type, payload, round_number, session_id))
        return results


@pytest_asyncio.fixture
async def asgi_router(cfg):
    apps = {
        "metrics": build_metrics_app(cfg),
        "log": build_log_app(),
        "routing": build_routing_app(),
        "config": build_config_app(cfg),
    }
    clients: dict[str, httpx.AsyncClient] = {}
    try:
        for agent_id, agent_app in apps.items():
            transport = httpx.ASGITransport(app=agent_app)
            clients[agent_id] = httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30.0)
        yield ASGITestRouter(clients)
    finally:
        for client in clients.values():
            await client.aclose()


@pytest_asyncio.fixture
async def engine(cfg, asgi_router):
    return NetCortexEngine(cfg, asgi_router)


@pytest_asyncio.fixture
async def mcp_server_module(engine, cfg):
    """Import mcp_server.server, initialize it against the real test engine,
    and reset its module-level globals afterwards (it's a singleton-style
    module, so tests must not leak task-store state into one another)."""
    from mcp_server import server as mod

    mod.initialize_server(engine, cfg)
    try:
        yield mod
    finally:
        mod._task_store = None
        mod._engine = None
        mod._engine_cfg = None


@pytest.fixture
def tool_fn():
    """MCP SDK tool decorators typically leave the original coroutine
    function callable directly; some wrap it and expose it via `.fn`.
    Support both without hardcoding an assumption about the SDK version."""
    def _tool_fn(tool):
        return getattr(tool, "fn", tool)
    return _tool_fn


@pytest_asyncio.fixture
async def poll_until_done():
    async def _poll(mod, task_id: str, timeout: float = 45.0, interval: float = 0.05) -> dict:
        get_report = getattr(mod.get_report, "fn", mod.get_report)
        elapsed = 0.0
        while elapsed < timeout:
            result = await get_report(task_id)
            if result.get("status") in ("completed", "failed", "cancelled"):
                return result
            await asyncio.sleep(interval)
            elapsed += interval
        raise TimeoutError(f"task {task_id} did not finish within {timeout}s")
    return _poll
