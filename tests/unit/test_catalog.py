"""Detection catalog: JSON Schema + model + cross-reference validation,
override merging, the validate CLI, and refusing to start on a bad catalog."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from contract.catalog_loader import (
    CATALOG_ENV,
    DEFAULT_CATALOG_DIR,
    SCHEMA_DIR,
    CatalogError,
    get_catalog,
    load_catalog,
    render_schemas,
    reset_catalog_cache,
)

REPO = Path(__file__).resolve().parents[2]
OVERRIDES = REPO / "tests" / "fixtures" / "catalog_overrides"


def _override(tmp_path: Path, filename: str, data: dict) -> Path:
    (tmp_path / filename).write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------- good

def test_default_catalog_is_valid_and_complete():
    cat = load_catalog()
    assert cat.domain_ids == ["metrics", "log", "routing", "config"]
    assert cat.active_domains("network") == ["metrics", "log", "routing", "config"]
    assert cat.active_domains("something-new") == cat.active_domains("unknown")
    assert cat.telemetry_domains() == {"metrics": "get_metrics", "logs": "get_logs",
                                       "routing": "get_routing_events", "config": "get_config_changes"}
    assert cat.signal("RevokeSecurityGroupIngress")[0] == "cloudtrail_network_mutation"
    assert cat.signal("NoSuchThing") is None


def test_each_default_file_validates_against_its_exported_schema():
    for filename in ("entities.json", "signals.json", "domains.json", "correlation.json", "sources.json"):
        schema = json.loads((SCHEMA_DIR / filename.replace(".json", ".schema.json")).read_text())
        jsonschema.validate(json.loads((DEFAULT_CATALOG_DIR / filename).read_text()), schema)


def test_exported_catalog_schemas_are_in_sync():
    for name, content in render_schemas().items():
        assert (SCHEMA_DIR / name).read_text(encoding="utf-8") == content, f"{name} stale: run export-schema"


def test_override_entry_wins_by_key_and_other_entries_are_kept(tmp_path):
    _override(tmp_path, "signals.json", {"signals": {
        "error_rate": {"signal_type": "metric", "domain": "metrics", "description": "overridden",
                       "rule": {"type": "threshold", "op": "gt", "value": 50}},
    }})
    cat = load_catalog(tmp_path)
    assert cat.signals.signals["error_rate"].description == "overridden"
    assert cat.signals.signals["error_rate"].rule.type == "threshold"   # whole entry replaced
    assert "packet_loss" in cat.signals.signals                           # untouched entries kept
    assert any(str(tmp_path) in o for o in cat.origin)


def test_get_catalog_reads_env_override(monkeypatch):
    monkeypatch.setenv(CATALOG_ENV, str(OVERRIDES / "dns"))
    reset_catalog_cache()
    try:
        assert "dns" in get_catalog().domain_ids
    finally:
        monkeypatch.delenv(CATALOG_ENV)
        reset_catalog_cache()
    assert "dns" not in get_catalog().domain_ids


# ----------------------------------------------------------------------- bad

@pytest.mark.parametrize("filename,data,expected", [
    ("signals.json", {"signals": {"x": {"signal_type": "gauge", "domain": "metrics", "description": "d"}}},
     "signals.x.signal_type: 'gauge' is not one of"),
    ("signals.json", {"signals": {"x": {"signal_type": "metric", "domain": "metrics", "description": "d",
                                        "rule": {"type": "eval", "expr": "value > 2"}}}},
     "signals.x.rule"),
    ("signals.json", {"signals": {"x": {"signal_type": "metric", "domain": "nope", "description": "d"}}},
     "signals.json: signals.x.domain: unknown domain 'nope'"),
    ("signals.json", {"signals": {"x": {"signal_type": "metric", "domain": "metrics", "description": "d",
                                        "rule": {"type": "threshold", "op": "between", "min": 1}}}},
     "signals.x.rule: op 'between' needs min and max"),
    ("signals.json", {"signals": {"x": {"signal_type": "event", "domain": "config", "description": "d",
                                        "match_names": ["RevokeSecurityGroupIngress"]}}},
     "'RevokeSecurityGroupIngress' already claimed"),
    ("domains.json", {"domains": {"metrics": {"telemetry_domain": "metrics", "tool": "get_dns"}}},
     "domains.metrics"),
    ("domains.json", {"degradation_map": {"network": ["metrics", "ghost"]}},
     "degradation_map.network: unknown domain(s) ['ghost']"),
    ("correlation.json", {"rules": {"r": {"domain": "metrics", "when": {"self_anomaly": True},
                                          "effect": {"kind": "corroboration", "confidence": {"op": "add", "value": 0.1},
                                                     "note": "no marker here"}}}},
     "correlation.json: rules.r.effect.note: must start with 'Peer corroboration:'"),
    ("correlation.json", {"rules": {"r": {"domain": "metrics", "when": {"self_anomaly": True, "all": [{}]},
                                          "effect": {"kind": "corroboration", "confidence": {"op": "add", "value": 0.1},
                                                     "note": "Peer corroboration: x"}}}},
     "rules.r.when.all.0: a condition must set exactly one"),
    ("sources.json", {"mappings": {"m": {"source": "simulation", "record": "metrics_data", "domain": "metrics",
                                         "signal_type": "metric", "entity": {"type": "x", "id_field": "y"},
                                         "emit": [{"name": "not_a_signal"}]}}},
     "sources.json: mappings.m.emit.0.name: 'not_a_signal' is not a catalog signal"),
    ("entities.json", {"relationships": {"r": {"source": ["tunnel"], "target": ["unicorn"]}}},
     "entities.json: relationships.r.target: unknown entity type(s) ['unicorn']"),
])
def test_invalid_catalog_names_file_and_field(tmp_path, filename, data, expected):
    with pytest.raises(CatalogError) as err:
        load_catalog(_override(tmp_path, filename, data))
    assert expected in str(err.value)
    assert filename in str(err.value)


def test_unknown_placeholder_in_summary_template_rejected(tmp_path):
    domains = json.loads((DEFAULT_CATALOG_DIR / "domains.json").read_text())["domains"]
    log = domains["log"]
    log["summary"]["clean"] = [{"template": "No log anomaly on {hostname}"}]
    with pytest.raises(CatalogError, match=r"domains.log.summary.clean.0.template: unknown placeholder\(s\) \['hostname'\]"):
        load_catalog(_override(tmp_path, "domains.json", {"domains": {"log": log}}))


def test_bad_json_and_unknown_override_file_rejected(tmp_path):
    (tmp_path / "signals.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(CatalogError, match="signals.json: line 1"):
        load_catalog(tmp_path)
    (tmp_path / "signals.json").unlink()
    (tmp_path / "signal.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CatalogError, match="unknown catalog file"):
        load_catalog(tmp_path)
    with pytest.raises(CatalogError, match="does not exist"):
        load_catalog(tmp_path / "missing")


# ----------------------------------------------------------------- CLI + boot

def _cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "app/main.py", *args], cwd=REPO, capture_output=True, text=True,
                          env=env, timeout=120)


def test_catalog_validate_cli_exit_codes(tmp_path):
    ok = _cli("catalog", "validate", str(OVERRIDES / "dns"))
    assert ok.returncode == 0 and "5 domains" in ok.stdout
    bad = _override(tmp_path, "domains.json", {"degradation_map": {"overload": []}})
    res = _cli("catalog", "validate", str(bad))
    assert res.returncode == 1
    assert "INVALID: domains.json: degradation_map.overload: must list at least one domain" in res.stdout


def test_engine_and_telemetry_server_refuse_to_start_on_invalid_catalog(tmp_path, monkeypatch):
    import os

    bad = _override(tmp_path, "signals.json", {"signals": {"x": {"signal_type": "metric", "domain": "nope", "description": "d"}}})
    env = {**os.environ, CATALOG_ENV: str(bad)}
    telemetry = subprocess.run([sys.executable, "-m", "mcp_telemetry_server", "--port", "0"], cwd=REPO,
                               capture_output=True, text=True, env=env, timeout=120)
    assert telemetry.returncode != 0
    assert "Invalid detection catalog: signals.json: signals.x.domain" in telemetry.stderr

    from core.orchestrator import NetCortexEngine

    monkeypatch.setenv(CATALOG_ENV, str(bad))
    reset_catalog_cache()
    try:
        cfg = {"a2a": {"message_timeout_seconds": 1, "round_timeout_seconds": 2, "collaboration_timeout_seconds": 10,
                       "analysis_timeout_seconds": 5, "max_iterations": 1}}
        with pytest.raises(CatalogError, match="signals.x.domain"):
            NetCortexEngine(cfg, router=None)  # type: ignore[arg-type]
    finally:
        monkeypatch.delenv(CATALOG_ENV)
        reset_catalog_cache()
