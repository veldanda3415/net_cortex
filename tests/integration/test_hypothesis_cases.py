"""LLM proposes, agents verify, end to end through the real agents and engine.

The sg-rule-removed replay case runs with a catalog that no longer knows
ModifySecurityGroupRules (evals/ablation.py). A scripted LLM stands in for
Gemini, so what is tested is the code path: the triage request, the four
checks in the config agent, the evidence, the ranking and the report."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents import rca_synthesizer, triage
from contract.catalog_loader import reset_catalog_cache
from evals.ablation import ABLATIONS, catalog_override
from evals.runner import cases_config
from models.schemas import IncidentRequest

CASES = Path(__file__).resolve().parents[2] / "evals" / "cases"
CASE = json.loads((CASES / "sg-rule-removed.json").read_text())
SG = "sg-0d47d22a6a536ef01"


def _with_cases(cfg: dict) -> None:
    cases = [json.loads(p.read_text()) for p in sorted(CASES.glob("*.json"))]
    cfg.update(cases_config(cfg, cases, window_minutes=15))
    cfg.pop("providers", None)


@pytest.fixture
def cfg(cfg, request):
    configure = getattr(request, "param", None)
    if configure is not None:
        configure(cfg)
    return cfg


@pytest.fixture
def ablated(tmp_path, monkeypatch):
    monkeypatch.setenv("NETCORTEX_CATALOG_DIR", str(catalog_override(ABLATIONS["sg-rule-removed"], tmp_path / "catalog")))
    reset_catalog_cache()
    yield
    monkeypatch.delenv("NETCORTEX_CATALOG_DIR")
    reset_catalog_cache()


def _llm(monkeypatch, rule: dict) -> list[str]:
    """LLM on, with a scripted triage answer citing the CloudTrail line it was shown."""
    prompts: list[str] = []

    def fake(prompt: str, model: str):
        prompts.append(prompt)
        refs = [line.split("refs=[", 1)[1].split("]", 1)[0] for line in prompt.splitlines()
                if "signal=ModifySecurityGroupRules" in line]
        if not refs:
            return {"hypotheses": []}
        return {"hypotheses": [{"evidence_refs": [refs[0]], "signal": "ModifySecurityGroupRules", "rule": rule,
                                "role": "cause", "why": "the web SG's on-prem rule was narrowed",
                                "proposed_name": "security_group_rule_modified"}]}

    monkeypatch.setattr(triage, "llm_enabled", lambda cfg: True)
    monkeypatch.setattr("core.orchestrator.llm_enabled", lambda cfg: True)
    monkeypatch.setattr(triage, "_call", fake)
    monkeypatch.setattr(rca_synthesizer, "_call_gemini", lambda prompt, model: None)   # deterministic synthesis
    return prompts


def _incident() -> IncidentRequest:
    return IncidentRequest(scenario_id=CASE["scenario_id"], description=CASE["description"], region=CASE["region"],
                           severity="high", reported_at=datetime.now(timezone.utc), alarm_entities=CASE["alarm_entities"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_cases], indirect=True)
async def test_rules_only_cannot_name_an_uncatalogued_change(ablated, engine):
    report = await engine.run_incident(_incident())
    assert report.ranked_causes and report.ranked_causes[0].entity_id != SG
    assert report.hypotheses == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_cases], indirect=True)
async def test_a_verified_hypothesis_names_the_culprit(ablated, engine, monkeypatch):
    prompts = _llm(monkeypatch, {"type": "event_presence", "min_severity": "info"})
    report = await engine.run_incident(_incident())

    assert any("signal=ModifySecurityGroupRules" in p for p in prompts)
    [h] = report.hypotheses
    assert h["verdict"] == "supported" and all(h["checks"].values()) and h["proposed_by"] == h["checked_by"] == "config"
    config = next(f for f in report.agent_findings if f.domain == "config")
    row = next(r for r in config.evidence if r.get("via") == "hypothesis")
    assert row["role"] is None and row["rules_fired"][0]["rule"] == h["rule_id"] and h["rule_id"].startswith("llm:")
    top = report.ranked_causes[0]
    assert (top.entity_type, top.entity_id) == ("security_group", SG) and top.via_hypothesis
    assert top.features["role"] == 0.5
    assert SG in report.root_cause and "LLM hypothesis the agents verified" in report.root_cause


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [_with_cases], indirect=True)
async def test_a_hypothesis_whose_rule_does_not_fire_stays_out_of_the_root_cause(ablated, engine, monkeypatch):
    _llm(monkeypatch, {"type": "state_equals", "field": "attributes.error_code", "value": "Client.Unauthorized"})
    report = await engine.run_incident(_incident())

    [h] = report.hypotheses
    assert h["verdict"] == "refuted" and h["checks"]["fires"] is False and h["checks"]["grounded"] is True
    assert not any(r.get("via") == "hypothesis" for f in report.agent_findings for r in f.evidence)
    assert report.ranked_causes[0].entity_id != SG and h["rule_id"] not in report.root_cause
