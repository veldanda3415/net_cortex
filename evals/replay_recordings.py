"""Re-run recorded live incidents offline, through the full pipeline.

    python -m evals.replay_recordings output/alarm_bridge/<incident>.json ... [--truth campaign-results.jsonl]

Each argument is a record the alarm bridge saved ({"incident", "report"}). Its
telemetry was recorded by the RCA side (telemetry.record_dir) as
<record_dir>/<region>_<reported_at>.json. The replay starts the domain agents
in-process (no ports), serves that recording through a replay source, and
runs the same incident again: same description, alarm entities and alarm
time relative to the run. Run it with the LLM off to get the rules-only answer
to the same evidence the live run saw:

    GEMINI_API_KEY= GOOGLE_CLOUD_PROJECT= python -m evals.replay_recordings ...

With --truth (the drill campaign's JSONL, one {"fault", "netcortex": {"truth"}}
per line) each replay is scored like infra/faults/score.py.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.domain_agent import build_all_domain_apps  # noqa: E402
from communication.a2a_router import A2ARouter  # noqa: E402
from communication.agent_registry import AgentRegistry, RegisteredAgent  # noqa: E402
from core.orchestrator import NetCortexEngine  # noqa: E402
from evals.scoring import headline_names_culprit, llm_used, load_expect, verdict  # noqa: E402
from models.schemas import IncidentRequest  # noqa: E402
from providers.adapters.mcp_adapter import close_all_sessions  # noqa: E402

STATUS_LAG = timedelta(minutes=5)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def recording_for(record: dict[str, Any], record_dir: Path) -> Path:
    incident = record["incident"]
    alarm = _ts(incident["reported_at"]).astimezone(timezone.utc)
    return record_dir / f"{incident.get('region', 'us-east-1')}_{alarm.strftime('%Y%m%dT%H%M%SZ')}.json"


def replay_config(base: dict[str, Any], fixture: Path, name: str) -> dict[str, Any]:
    cfg = copy.deepcopy(base)
    url = f"inproc://replay-{name}"
    endpoint = [{"name": f"replay-{name}", "url": url, "required": True}]
    cfg["telemetry"] = {
        "inproc_sources": {"replay": {"enabled": True, "fixtures": [str(fixture)]}},
        "domains": {d: {"endpoints": endpoint} for d in ("metrics", "logs", "routing", "config")},
        "topology": {"endpoints": endpoint},
    }
    cfg.setdefault("llm", {})["require_success"] = False
    return cfg


async def replay(record: dict[str, Any], fixture: Path, base_cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = replay_config(base_cfg, fixture, record["report"]["incident_id"][:8])
    apps = build_all_domain_apps(cfg)
    registry = AgentRegistry()
    mounts = {}
    for agent_id, app in apps.items():
        registry.agents[agent_id] = RegisteredAgent(agent_id=agent_id, endpoint=f"http://{agent_id}/a2a", card={})
        mounts[f"http://{agent_id}"] = httpx.ASGITransport(app=app)
    engine = NetCortexEngine(cfg, A2ARouter(registry, int(cfg["a2a"]["message_timeout_seconds"]), mounts=mounts))
    original = record["incident"]
    incident = IncidentRequest(
        description=original["description"], region=original.get("region", "us-east-1"),
        severity=original.get("severity", "high"), source_system="replay",
        external_incident_id=f"REPLAY-{record['report']['incident_id']}",
        reported_at=datetime.now(timezone.utc) - STATUS_LAG,
        alarm_entities=original.get("alarm_entities") or [],
    )
    report = await engine.run_incident(incident)
    return report.model_dump(mode="json")


async def main(argv: list[str] | None = None) -> int:
    import yaml

    parser = argparse.ArgumentParser(prog="python -m evals.replay_recordings")
    parser.add_argument("records", nargs="+")
    parser.add_argument("--config", default="config/config.aws-lab.yaml")
    parser.add_argument("--record-dir", default="output/recordings")
    parser.add_argument("--truth", help="campaign results JSONL, to score each replay")
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    base = yaml.safe_load(Path(args.config).read_text())
    truth: dict[str, dict[str, Any]] = {}
    if args.truth:
        for line in Path(args.truth).read_text().splitlines():
            row = json.loads(line)
            nc = row.get("netcortex") or {}
            if nc.get("incident_id"):
                truth[nc["incident_id"]] = row
    expect = load_expect()
    out = sys.stdout if args.out == "-" else open(args.out, "a")
    try:
        for path in args.records:
            record = json.loads(Path(path).read_text())
            fixture = recording_for(record, Path(args.record_dir))
            if not fixture.exists():
                print(json.dumps({"record": path, "error": f"no recording {fixture}"}), file=out, flush=True)
                continue
            report = await replay(record, fixture, base)
            row: dict[str, Any] = {"record": path, "live_incident_id": record["report"]["incident_id"],
                                   "root_cause": report["root_cause"][:300], "incident_status": report["incident_status"],
                                   "top_cause": (lambda c: f"{c['entity_type']}:{c['entity_id']} ({c['domain']})" if c else None)(
                                       (report.get("ranked_causes") or [None])[0]),
                                   "questions": sum(1 for m in report["a2a_message_log"] if m["message_type"] == "validation_request"),
                                   "llm_used": llm_used(report)}
            live = truth.get(record["report"]["incident_id"])
            if live is not None and live["fault"] in expect:
                fault_expect = expect[live["fault"]]
                v, cause, symptoms = verdict(fault_expect, report)
                row.update(fault=live["fault"], verdict=v, cause_fired=cause, symptoms_fired=symptoms,
                           headline_cause=headline_names_culprit(fault_expect, report, cause))
            print(json.dumps(row), file=out, flush=True)
    finally:
        await close_all_sessions()
        if out is not sys.stdout:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
