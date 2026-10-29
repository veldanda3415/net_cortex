"""`app/main.py eval --cases <dir>`: run replay cases through the full pipeline.

Every case names a replay fixture (evals/build_fixtures.py) recorded around
one alarm. The runner starts the normal runtime (domain agents, A2A router,
engine) against an in-process telemetry server whose only source is replay,
with every case's fixture loaded; a fixture answers only its own case's
scenario_id. Each case becomes an incident whose reported_at is the alarm
(`minutes_since_alarm` ago, so post-alarm samples exist when a case needs
them), and the report is scored with evals/scoring.py.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from evals.scoring import load_expect, score_case
from models.schemas import IncidentRequest

CASES_URL = "inproc://eval-cases"


def load_cases(cases_dir: str | Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(Path(cases_dir).glob("*.json"))]


def cases_config(cfg: dict[str, Any], cases: list[dict[str, Any]], window_minutes: int) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    endpoint = [{"name": "replay", "url": CASES_URL, "required": True}]
    out["telemetry"] = {
        **(cfg.get("telemetry") or {}),
        "inproc_sources": {"replay": {"enabled": True, "fixtures": [c["fixture"] for c in cases]}},
        "domains": {d: {"endpoints": endpoint} for d in ("metrics", "logs", "routing", "config")},
        "topology": {"endpoints": endpoint},
    }
    out.setdefault("simulation", {})["window_minutes"] = window_minutes
    return out


async def run_cases(
    cfg: dict[str, Any],
    cases_dir: str | Path,
    start_runtime: Callable[[dict[str, Any]], Awaitable[tuple[list, Any]]],
    write_outputs: Callable[..., None],
    window_minutes: int = 15,
) -> list[dict[str, Any]]:
    from providers.adapters.mcp_adapter import close_all_sessions

    cases = load_cases(cases_dir)
    if not cases:
        raise ValueError(f"no case files in {cases_dir}")
    expect = load_expect()
    tasks, engine = await start_runtime(cases_config(cfg, cases, window_minutes))
    results = []
    try:
        for case in cases:
            reported_at = datetime.now(timezone.utc) - timedelta(minutes=int(case.get("minutes_since_alarm", 0)))
            incident = IncidentRequest(
                scenario_id=case["scenario_id"], description=case["description"], region=case["region"],
                severity=case.get("severity", "high"), source_system="eval-case",
                external_incident_id=f"CASE-{case['name']}", reported_at=reported_at,
                alarm_entities=case.get("alarm_entities", []),
            )
            report = await engine.run_incident(incident)
            report_data = report.model_dump(mode="json")
            write_outputs(report_data, incident.incident_id, engine.last_result_state)
            results.append({**score_case(case, report_data, expect), "incident_id": incident.incident_id,
                            "why": case.get("why", "")})
    finally:
        await close_all_sessions()
        for t in tasks:
            t.cancel()
        import asyncio

        await asyncio.gather(*tasks, return_exceptions=True)
    return results


def print_results(results: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 100)
    print("NetCortex cross-domain case eval")
    print("=" * 100)
    for r in results:
        flags = []
        for key in ("cross_domain", "culprit_top1", "culprit_in_root_cause", "status_ok"):
            if key in r:
                flags.append(f"{key}={'yes' if r[key] else 'NO'}")
        if "blamed_config" in r:
            flags.append(f"blamed_config={'YES' if r['blamed_config'] else 'no'}")
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['case']:<24} verdict={r['verdict']:<8} status={r['status']:<9} "
              f"questions={r['questions']} supported={r['supported']} " + " ".join(flags))
        print(f"     top cause: {r['top_cause']}")
        print(f"     root cause: {r['root_cause'][:180]}")
        print(f"     output: output/{r['incident_id']}/rca_report.json")
    with_culprit = [r for r in results if "culprit_top1" in r]
    cross = [r for r in with_culprit if r.get("cross_domain")]
    print("-" * 100)
    print(f"Passed {sum(r['passed'] for r in results)}/{len(results)} | culprit top-1 {sum(r['culprit_top1'] for r in with_culprit)}/{len(with_culprit)} "
          f"(cross-domain {sum(r['culprit_top1'] for r in cross)}/{len(cross)}) | culprit named in root cause "
          f"{sum(r['culprit_in_root_cause'] for r in with_culprit)}/{len(with_culprit)}")
    print("=" * 100 + "\n")
