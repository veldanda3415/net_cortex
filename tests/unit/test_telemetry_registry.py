"""RCA-side telemetry registry: endpoint fan-out, merge, reliability controls."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from contract import TelemetryQuery
from mcp_telemetry_server.server import build_server
from mcp_telemetry_server.service import TelemetryService
from providers import resilience
from providers.adapters.mcp_adapter import EndpointConfig, MCPEndpointClient
from providers.factory import TelemetryRegistry, create_telemetry_provider, parse_endpoints
from providers.inproc import ensure_inproc_server, register_inproc_server, unregister_inproc_server
from providers.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, is_transport_error

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay"
DOWN_URL = "http://127.0.0.1:1/mcp"  # nothing listens on port 1: connection refused


def _query(region: str = "us-east", scenario_id: int | None = 1) -> TelemetryQuery:
    end = datetime.now(timezone.utc)
    return TelemetryQuery(region=region, start_time=end - timedelta(minutes=30), end_time=end, scenario_id=scenario_id)


def _cfg(endpoints: list[dict], domain: str = "metrics") -> dict:
    return {"telemetry": {"domains": {domain: {"endpoints": endpoints}}}}


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(resilience, "sleep", fake_sleep)
    return sleeps


@pytest.fixture
def replay_endpoint():
    url = "inproc://replay-ci"
    register_inproc_server(url, build_server(TelemetryService(
        {"replay": {"enabled": True, "fixtures_dir": str(FIXTURES)}}, allow_cloud_sources=False)))
    yield url
    unregister_inproc_server(url)


# ---------------------------------------------------------------------------
# Two endpoints for one domain, one down
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_endpoints_one_down_returns_data_with_partial_coverage():
    registry = create_telemetry_provider(_cfg([
        {"name": "primary", "url": "inproc://local"},
        {"name": "secondary", "url": DOWN_URL, "timeout_seconds": 2, "retry": {"max_attempts": 2}},
    ]), domains=("metrics",))

    response = await registry.fetch("metrics", _query())

    assert len(response.signals) == 16  # 4 snapshots x 4 metrics from the healthy endpoint
    assert response.status == "partial"
    assert response.degraded
    by_name = {c.source: c for c in response.coverage}
    assert set(by_name) == {"primary", "secondary"}  # one coverage entry per endpoint
    assert by_name["primary"].status == "ok"
    assert by_name["secondary"].status == "failed"
    assert "ConnectError" in by_name["secondary"].error and "2 attempt(s)" in by_name["secondary"].error


@pytest.mark.asyncio
async def test_optional_endpoint_down_is_partial_but_not_degraded():
    registry = create_telemetry_provider(_cfg([
        {"name": "primary", "url": "inproc://local"},
        {"name": "enrichment", "url": DOWN_URL, "required": False, "retry": {"max_attempts": 1}},
    ]), domains=("metrics",))
    response = await registry.fetch("metrics", _query())
    assert response.status == "partial"
    assert not response.degraded


@pytest.mark.asyncio
async def test_all_endpoints_down_is_failed_and_degraded():
    registry = create_telemetry_provider(_cfg([{"name": "only", "url": DOWN_URL, "retry": {"max_attempts": 1}}]),
                                         domains=("metrics",))
    response = await registry.fetch("metrics", _query())
    assert response.signals == [] and response.status == "failed" and response.degraded


# ---------------------------------------------------------------------------
# Config-only second endpoint + dedupe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_endpoint_added_through_config_only(replay_endpoint):
    one = create_telemetry_provider(_cfg([{"name": "sim", "url": "inproc://local"}]), domains=("metrics",))
    two = create_telemetry_provider(_cfg([
        {"name": "sim", "url": "inproc://local"},
        {"name": "replay", "url": replay_endpoint},
    ]), domains=("metrics",))

    query = _query(region="us-east-1")  # replay fixture is us-east-1; simulation has no us-east-1 data
    r1, r2 = await one.fetch("metrics", query), await two.fetch("metrics", query)

    assert [c.source for c in r1.coverage] == ["sim"]
    assert [c.source for c in r2.coverage] == ["sim", "replay"]
    assert all(c.status == "ok" for c in r2.coverage)
    assert {s.source for s in r2.signals} == {"cloudwatch"} and len(r2.signals) == 2


@pytest.mark.asyncio
async def test_signals_deduped_on_source_and_evidence_ref_across_endpoints():
    ensure_inproc_server("inproc://local", None)
    registry = create_telemetry_provider(_cfg([
        {"name": "a", "url": "inproc://local"},
        {"name": "b-same-backend", "url": "inproc://local"},
    ]), domains=("metrics",))
    response = await registry.fetch("metrics", _query())
    assert len(response.signals) == 16  # not 32
    assert [c.status for c in response.coverage] == ["ok", "ok"]


def test_endpoint_config_validation():
    with pytest.raises(ValueError, match="duplicate endpoint names"):
        parse_endpoints(_cfg([{"name": "x", "url": "inproc://local"}, {"name": "x", "url": DOWN_URL}]))
    with pytest.raises(ValueError, match="needs a url"):
        parse_endpoints(_cfg([{"name": "x"}]))
    with pytest.raises(ValueError, match="oidc is not implemented"):
        parse_endpoints(_cfg([{"name": "x", "url": DOWN_URL, "auth": "oidc"}]))
    with pytest.raises(ValueError, match="token_env"):
        parse_endpoints(_cfg([{"name": "x", "url": DOWN_URL, "auth": "bearer_env"}]))
    with pytest.raises(ValueError, match="unknown domain"):
        parse_endpoints({"telemetry": {"domains": {"traces": {"endpoints": []}}}})


def test_defaults_apply_and_endpoint_overrides_win():
    parsed = parse_endpoints({"telemetry": {
        "defaults": {"timeout_seconds": 5, "retry": {"max_attempts": 4}, "circuit_breaker": {"failure_threshold": 7}},
        "domains": {"logs": {"endpoints": [{"name": "x", "url": DOWN_URL, "retry": {"max_attempts": 1}}]}},
    }})
    ep = parsed["logs"][0]
    assert (ep.timeout_seconds, ep.retry.max_attempts, ep.failure_threshold) == (5, 1, 7)
    assert parsed["metrics"][0].url == "inproc://local"  # unspecified domains default to in-process simulation


def test_legacy_providers_config_still_accepted():
    parsed = parse_endpoints({"providers": {"metrics": "mcp", "logs": "simulation"},
                              "mcp_endpoints": {"metrics": {"url": "http://t:9001/mcp", "timeout_seconds": 3}}})
    assert parsed["metrics"][0].url == "http://t:9001/mcp" and parsed["metrics"][0].timeout_seconds == 3
    assert parsed["logs"][0].url == "inproc://local"


@pytest.mark.parametrize("name", ["cloudwatch", "cloudwatch_logs", "cloudtrail", "tgw_routes"])
def test_inproc_server_refuses_cloud_plugins(name):
    with pytest.raises(ValueError, match="telemetry pod"):
        TelemetryService({name: {"enabled": True}}, allow_cloud_sources=False)


# ---------------------------------------------------------------------------
# Contract version on the client side
# ---------------------------------------------------------------------------

def _payload(version: str) -> SimpleNamespace:
    end = datetime.now(timezone.utc)
    body = {"contract_version": version, "window": {"start": (end - timedelta(minutes=30)).isoformat(), "end": end.isoformat()},
            "coverage": [{"source": "x", "status": "ok"}], "signals": []}
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(body))], is_error=False, structured_content=None)


class _Fake:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def factory(self):
        @asynccontextmanager
        async def cm():
            yield self
        return cm()

    async def call_tool(self, name, args):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def _client(fake: _Fake, **overrides) -> MCPEndpointClient:
    cfg = EndpointConfig(name="ep", url="http://ep/mcp", **overrides)
    return MCPEndpointClient(cfg, client_factory=fake.factory)


@pytest.mark.asyncio
async def test_major_contract_version_mismatch_in_response_is_rejected():
    fake = _Fake([_payload("3.1")])
    registry = TelemetryRegistry({"metrics": [_client(fake)]})
    response = await registry.fetch("metrics", _query())
    assert response.coverage[0].status == "failed"
    assert "Unsupported contract_version '3.1'" in response.coverage[0].error
    assert fake.calls == 1  # not a transport error: no retry


# ---------------------------------------------------------------------------
# Retry + circuit breaker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transport_errors_retried_with_jitter_then_succeed(_no_backoff_sleep):
    fake = _Fake([ConnectionError("reset"), _payload("2.0")])
    client = _client(fake, retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=2))
    response, _ = await client.fetch("metrics", _query())
    assert response.status == "ok" and fake.calls == 2
    assert len(_no_backoff_sleep) == 1 and 0 <= _no_backoff_sleep[0] <= 0.5


@pytest.mark.asyncio
async def test_tool_errors_are_not_retried():
    error = SimpleNamespace(content=[SimpleNamespace(text="bad args")], is_error=True, structured_content=None)
    fake = _Fake([error])
    with pytest.raises(Exception, match="bad args"):
        await _client(fake, retry=RetryPolicy(max_attempts=3)).fetch("metrics", _query())
    assert fake.calls == 1


def test_retry_delay_is_bounded_full_jitter():
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.2, max_delay_seconds=1.0)
    assert policy.delay(1, rng=lambda: 1.0) == pytest.approx(0.2)
    assert policy.delay(3, rng=lambda: 1.0) == pytest.approx(0.8)
    assert policy.delay(10, rng=lambda: 1.0) == pytest.approx(1.0)  # capped
    assert policy.delay(3, rng=lambda: 0.0) == 0.0


def test_transport_error_classification_unwraps_exception_groups():
    import httpx2

    assert is_transport_error(ExceptionGroup("tg", [httpx2.ConnectError("refused")]))
    assert is_transport_error(TimeoutError())
    assert not is_transport_error(ValueError("bad payload"))


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_recovers():
    now = [1000.0]
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=30, clock=lambda: now[0])
    fake = _Fake([ConnectionError("down")])
    client = MCPEndpointClient(EndpointConfig(name="ep", url="http://ep/mcp", retry=RetryPolicy(max_attempts=1)),
                               client_factory=fake.factory, breaker=breaker)
    registry = TelemetryRegistry({"metrics": [client]})

    for _ in range(3):
        assert (await registry.fetch("metrics", _query())).coverage[0].status == "failed"
    assert breaker.state == "open" and fake.calls == 3

    # Open: calls short-circuit without touching the endpoint.
    response = await registry.fetch("metrics", _query())
    assert "circuit open" in response.coverage[0].error and fake.calls == 3

    # After the reset timeout one trial is allowed; it fails -> open again for a full timeout.
    now[0] += 30
    assert breaker.state == "half_open"
    await registry.fetch("metrics", _query())
    assert fake.calls == 4 and breaker.state == "open"
    now[0] += 10
    await registry.fetch("metrics", _query())
    assert fake.calls == 4  # still open

    # Endpoint recovers; next trial succeeds and closes the breaker.
    fake.results = [_payload("2.0")]
    now[0] += 30
    response = await registry.fetch("metrics", _query())
    assert response.coverage[0].status == "ok" and breaker.state == "closed"
    await registry.fetch("metrics", _query())
    assert fake.calls == 6


def test_half_open_allows_a_single_trial():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=5, clock=lambda: now[0])
    breaker.before_call()
    breaker.record_failure()
    now[0] = 5
    assert breaker.before_call() is True  # the trial
    with pytest.raises(CircuitOpenError):
        breaker.before_call()  # concurrent caller during the trial
    breaker.release_trial()
    assert breaker.before_call() is True
