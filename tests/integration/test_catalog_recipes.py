"""The "Recipes" in CONTRACT.md's Detection catalog section, run for real.

Each recipe is an override directory under
tests/fixtures/catalog_overrides/recipes/. Every test runs an incident
twice through the real engine: once on the default catalog (the recipe's
effect must be absent) and once with the override (the effect must appear).
test_contract_md_recipe_snippets_match_fixtures keeps the JSON shown in
CONTRACT.md identical to these fixtures, so the docs can't drift.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from contract.catalog_loader import CATALOG_ENV, reset_catalog_cache
from models.schemas import IncidentRequest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
RECIPES = FIXTURES / "catalog_overrides" / "recipes"


def _with_replay(cfg: dict) -> None:
    # Simulation plus the replay plugin serving the recipe fixtures, for every domain.
    url = "inproc://recipes-e2e"
    cfg["telemetry"] = {
        "inproc_sources": {
            "simulation": {"enabled": True},
            "replay": {"enabled": True, "fixtures_dir": str(FIXTURES / "replay_recipes")},
        },
        "domains": {d: {"endpoints": [{"name": "telemetry", "url": url}]}
                    for d in ("metrics", "logs", "routing", "config")},
    }


def _defaults(cfg: dict) -> None:
    return None


def _defaults_with_replay(cfg: dict) -> None:
    _with_replay(cfg)


def _recipe(name: str, replay: bool = False):
    def configure(cfg: dict) -> str:
        if replay:
            _with_replay(cfg)
        return str(RECIPES / name)
    configure.__name__ = name
    return configure


@pytest.fixture
def cfg(cfg, request, monkeypatch):
    configure = getattr(request, "param", None)
    reset_catalog_cache()
    override = configure(cfg) if configure is not None else None
    if override:
        monkeypatch.setenv(CATALOG_ENV, override)
    yield cfg
    monkeypatch.delenv(CATALOG_ENV, raising=False)
    reset_catalog_cache()


def _incident(scenario_id: int, description: str) -> IncidentRequest:
    return IncidentRequest(scenario_id=scenario_id, description=description, region="us-east", severity="high")


def _finding(report, domain):
    return next(f for f in report.agent_findings if f.domain == domain)


def _rules(finding) -> list[str]:
    return [r["rule"] for e in finding.key_events for r in e.get("rules_fired", [])]


BGP = "BGP route withdrawal"   # scenario 2: the only log line is WARN "BGP session flap"
VOIP = "VoIP latency spike"    # scenario 3: metrics and logs clean on the default catalog
LAG = "Throughput halved on LAG0"  # scenario 4: throughput drop + ERROR "LACP timeout on LAG0 member eth1/2"


# --- Recipe: treat WARN log lines as anomalous --------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_defaults], indirect=True)
async def test_warn_logs_default_ignores_warn(engine):
    assert not _finding(await engine.run_incident(_incident(2, BGP)), "log").anomaly_detected


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_recipe("warn_logs")], indirect=True)
async def test_warn_logs_recipe_flags_warn(engine):
    log = _finding(await engine.run_incident(_incident(2, BGP)), "log")
    assert log.anomaly_detected
    assert _rules(log) == ["log_message.event_presence"]
    assert log.key_events[0]["message"] == "BGP session flap - peer 203.0.113.1"


# --- Recipe: a new named log error (syslog mnemonic) --------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_defaults_with_replay], indirect=True)
async def test_syslog_default_keeps_mnemonic_unclassified(engine):
    log = _finding(await engine.run_incident(_incident(3, VOIP)), "log")
    assert not log.anomaly_detected
    assert {u["name"] for u in log.unclassified_evidence} == {"LINK-3-UPDOWN", "SYS-5-CONFIG_I"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_recipe("syslog_link_down", replay=True)], indirect=True)
async def test_syslog_recipe_detects_link_down(engine):
    log = _finding(await engine.run_incident(_incident(3, VOIP)), "log")
    assert log.anomaly_detected
    fired = [r for e in log.key_events for r in e.get("rules_fired", [])]
    assert [(r["signal"], r["rule"]) for r in fired] == [("LINK-3-UPDOWN", "link_down.event_presence")]
    assert fired[0]["evidence_ref"] == "syslog:sw-acc-07@2026-09-14T09:52:00Z#1"
    # The uncatalogued noise line is still surfaced, and still not judged.
    assert [u["name"] for u in log.unclassified_evidence] == ["SYS-5-CONFIG_I"]


# --- Recipe: a new AWS state metric (Direct Connect) --------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_defaults_with_replay], indirect=True)
async def test_dx_default_does_not_judge_connection_state(engine):
    metrics = _finding(await engine.run_incident(_incident(3, VOIP)), "metrics")
    assert not metrics.anomaly_detected
    assert "ConnectionState" in {u["name"] for u in metrics.unclassified_evidence}


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_recipe("dx_connection_down", replay=True)], indirect=True)
async def test_dx_recipe_detects_connection_down(engine):
    report = await engine.run_incident(_incident(3, VOIP))
    metrics = _finding(report, "metrics")
    assert metrics.anomaly_detected
    assert _rules(metrics) == ["ConnectionState.state_equals"]   # only the 0 sample, not the earlier 1
    assert metrics.key_events[0]["entity_id"] == "dxcon-fgabc123"
    assert metrics.unclassified_evidence == []


# --- Recipe: a new cross-domain corroboration rule ----------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_defaults], indirect=True)
async def test_lacp_default_has_no_lacp_corroboration(engine):
    metrics = _finding(await engine.run_incident(_incident(4, LAG)), "metrics")
    assert metrics.anomaly_detected
    assert "LACP" not in metrics.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_recipe("lacp_corroboration")], indirect=True)
async def test_lacp_recipe_corroborates_metrics(engine):
    metrics = _finding(await engine.run_incident(_incident(4, LAG)), "metrics")
    assert metrics.revised
    assert metrics.confidence >= 0.9
    assert metrics.summary.endswith("Peer corroboration: log agent reported an LACP error; metrics confidence elevated to 0.90")
    assert metrics.summary.count("Peer corroboration:") == 1   # idempotent across rounds


# --- Docs stay in sync ---------------------------------------------------------

_SNIPPET = re.compile(r"<!-- recipe: (?P<path>\S+) -->\s*```json\n(?P<body>.*?)```", re.S)


def test_contract_md_recipe_snippets_match_fixtures():
    snippets = list(_SNIPPET.finditer((ROOT / "CONTRACT.md").read_text(encoding="utf-8")))
    assert snippets, "no <!-- recipe: ... --> snippets found in CONTRACT.md"
    shown = {m["path"] for m in snippets}
    for m in snippets:
        assert json.loads(m["body"]) == json.loads((ROOT / m["path"]).read_text(encoding="utf-8")), m["path"]
    recipe_files = {p.relative_to(ROOT).as_posix() for p in RECIPES.rglob("*.json")}
    assert recipe_files <= shown, f"recipe fixtures not shown in CONTRACT.md: {sorted(recipe_files - shown)}"
