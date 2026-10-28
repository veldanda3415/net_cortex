"""Plumbing collaboration relies on: routers keep reply data, the recorder
writes replay fixtures, the LLM may only cite rule-fired evidence."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from agents import rca_synthesizer
from communication.a2a_router import A2ARouter
from communication.agent_registry import AgentRegistry, RegisteredAgent
from contract import Signal, TelemetryQuery, ToolResponse, TopologyResponse
from mcp_telemetry_server.plugins.replay import ReplayPlugin
from models.schemas import AgentFinding, RankedCause
from providers.recorder import TelemetryRecorder

NOW = datetime(2026, 10, 28, 5, 32, tzinfo=timezone.utc)


def test_send_direct_keeps_the_reply_data(monkeypatch):
    registry = AgentRegistry()
    registry.agents["config"] = RegisteredAgent(agent_id="config", endpoint="http://config/a2a", card={})
    router = A2ARouter(registry, message_timeout_seconds=5)

    async def post(endpoint, payload):
        return {"result": {"status": {"state": "completed"},
                           "artifacts": [{"parts": [{"kind": "data", "data": {"request_id": "r1", "verdict": "supported"}}]}]}}

    monkeypatch.setattr(router, "_post_task", post)
    message = asyncio.run(router.send_direct("log", "config", "validation_request", {"request_id": "r1"}, 1, "inc"))
    assert message.payload["status"] == "completed"
    assert message.payload["response"] == {"request_id": "r1", "verdict": "supported"}


def _signal(minute: int, ref: str) -> Signal:
    t = NOW + timedelta(minutes=minute)
    return Signal(signal_type="metric", timestamp=t, observed_at=t, entity={"type": "flow", "id": "f1"},
                  name="SuccessRate", value=0.0, domain="metrics", source="cloudwatch", evidence_ref=ref)


def test_recording_replays_the_same_signals(tmp_path):
    recorder = TelemetryRecorder(tmp_path)
    query = TelemetryQuery(region="us-east-1", start_time=NOW - timedelta(minutes=15), end_time=NOW, anchor_time=NOW)
    response = ToolResponse(window=query.window, signals=[_signal(-3, "a"), _signal(-2, "b")])
    recorder.record(query, response)
    recorder.record(query, response)  # a second agent reading the same data adds nothing
    later = query.model_copy(update={"start_time": NOW, "end_time": NOW + timedelta(minutes=5)})
    recorder.record(later, ToolResponse(window=later.window, signals=[_signal(2, "c")]))
    recorder.record_topology(TopologyResponse(region="us-east-1", entities=[{"type": "flow", "id": "f1"}]))

    path = recorder.path_for("us-east-1", NOW)
    data = json.loads(path.read_text())
    assert [s["evidence_ref"] for s in data["signals"]] == ["a", "b", "c"]
    assert data["alarm_at"].startswith("2026-10-28T05:32") and data["topology"]["entities"][0]["id"] == "f1"

    # Replayed for an alarm a day later, the spacing around the alarm is kept.
    plugin = ReplayPlugin({"fixtures": [str(path)]})
    alarm = NOW + timedelta(days=1)
    replay_query = TelemetryQuery(region="us-east-1", start_time=alarm - timedelta(minutes=15),
                                  end_time=alarm + timedelta(minutes=5), anchor_time=alarm)
    signals, _ = asyncio.run(plugin.fetch(replay_query, domain="metrics"))
    assert sorted(s.timestamp - alarm for s in signals) == [timedelta(minutes=-3), timedelta(minutes=-2), timedelta(minutes=2)]


def _anomalous() -> list[AgentFinding]:
    row = {"entity_type": "hosted_zone", "entity_id": "Z1", "signals": ["ChangeResourceRecordSets"], "role": "cause",
           "severity": "info", "first_seen": NOW.isoformat(), "last_seen": NOW.isoformat(), "count": 1,
           "rules_fired": [{"reason": "write call"}], "evidence_refs": ["cloudtrail:1"]}
    return [AgentFinding(agent_id="config", domain="config", anomaly_detected=True, summary="config change",
                         key_events=[], start_time=NOW, end_time=NOW, confidence=0.9, evidence=[row])]


def _ranked() -> list[RankedCause]:
    base = dict(domain="config", signals=["x"], evidence_refs=[], first_seen=NOW, features={"topology": 1.0})
    return [RankedCause(id="C1", entity_type="hosted_zone", entity_id="Z1", score=0.9, **base),
            RankedCause(id="C2", entity_type="route_table", entity_id="rtb-1", score=0.5, **base)]


def test_llm_root_cause_without_citations_falls_back(monkeypatch):
    monkeypatch.setattr(rca_synthesizer, "_call_gemini", lambda prompt, model: {
        "root_cause": "Someone deleted a DNS record.", "ranked_cause_ids": ["C2", "C9"]})
    report = rca_synthesizer.synthesize_report("inc", _anomalous(), [], incident_description="flow down",
                                               ranked_causes=_ranked(), alarm_entities=[{"type": "flow", "id": "f"}])
    assert report.root_cause.startswith("Config domain: x on hosted_zone Z1")  # deterministic: no citation
    # C2 scores 0.5 against the top's 0.9: the LLM cannot promote it.
    assert [c.id for c in report.ranked_causes] == ["C1", "C2"]


def test_llm_may_only_break_near_ties(monkeypatch):
    tied = [_ranked()[0], _ranked()[1].model_copy(update={"score": 0.88})]
    monkeypatch.setattr(rca_synthesizer, "_call_gemini", lambda prompt, model: {
        "root_cause": "Route rtb-1 was replaced [E1].", "ranked_cause_ids": ["C2", "C1"]})
    report = rca_synthesizer.synthesize_report("inc", _anomalous(), [], incident_description="flow down", ranked_causes=tied)
    assert [c.id for c in report.ranked_causes] == ["C2", "C1"]
    assert report.root_cause == "Route rtb-1 was replaced [E1]."


def test_llm_root_cause_must_name_the_top_cause(monkeypatch):
    monkeypatch.setattr(rca_synthesizer, "_call_gemini", lambda prompt, model: {
        "root_cause": "A tunnel went down [E1].", "ranked_cause_ids": ["C1"]})
    report = rca_synthesizer.synthesize_report("inc", _anomalous(), [], incident_description="flow down", ranked_causes=_ranked())
    assert report.root_cause.startswith("Config domain: x on hosted_zone Z1")


def test_llm_root_cause_with_known_citation_is_kept(monkeypatch):
    monkeypatch.setattr(rca_synthesizer, "_call_gemini", lambda prompt, model: {
        "root_cause": "The A record for app was deleted from zone Z1 [E1].", "ranked_cause_ids": ["C1"]})
    report = rca_synthesizer.synthesize_report("inc", _anomalous(), [], incident_description="flow down",
                                               ranked_causes=_ranked(), status="recovered")
    assert report.root_cause.endswith("[E1].") and report.incident_status == "recovered"
    assert "E1:" in rca_synthesizer._build_llm_prompt("flow down", _anomalous(), [], ranked=_ranked())
