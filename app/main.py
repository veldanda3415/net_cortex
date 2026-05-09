from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import typer
import uvicorn
import yaml

# Support direct execution: `python app/main.py`.
if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from agents.config_agent import build_config_app
from agents.log_agent import build_log_app
from agents.metrics_agent import build_metrics_app
from agents.routing_agent import build_routing_app
from communication.a2a_router import A2ARouter
from communication.adk_a2a_router import ADKA2ARouter
from communication.agent_registry import AgentRegistry
from ingestion.webhook_server import WebhookServer
from models.schemas import IncidentRequest
from core.orchestrator import NetCortexEngine
from simulation.scenarios import SCENARIOS

app = typer.Typer(help="NetCortex runtime")
logger = logging.getLogger("net_cortex")


def _bind_logger(name: str, level: int, formatter: logging.Formatter, file_path: Path) -> None:
    named = logging.getLogger(name)
    named.setLevel(level)
    named.propagate = False
    named.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    named.addHandler(console)

    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    named.addHandler(file_handler)


def configure_runtime_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_dir = Path("output") / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    _bind_logger("net_cortex", level, formatter, log_dir / "runtime.log")
    _bind_logger("net_cortex.orchestrator", level, formatter, log_dir / "orchestrator.log")
    _bind_logger("net_cortex.synthesizer", level, formatter, log_dir / "synthesizer.log")
    _bind_logger("net_cortex.agent.metrics", level, formatter, log_dir / "metrics.log")
    _bind_logger("net_cortex.agent.log", level, formatter, log_dir / "log.log")
    _bind_logger("net_cortex.agent.routing", level, formatter, log_dir / "routing.log")
    _bind_logger("net_cortex.agent.config", level, formatter, log_dir / "config.log")

    # Keep third-party network chatter out of console and files.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


def load_env_file(path: str = "config/.env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_config(path: str = "config/config.yaml") -> dict[str, Any]:
    load_env_file("config/.env")
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


async def start_server(fastapi_app, host: str, port: int):
    cfg = uvicorn.Config(fastapi_app, host=host, port=port, log_level="critical")
    server = uvicorn.Server(cfg)
    await server.serve()


async def start_runtime(cfg: dict[str, Any]):
    logger.info("Starting domain agent services on ports 8001-8004")
    tasks = [
        asyncio.create_task(start_server(build_metrics_app(cfg), "0.0.0.0", 8001)),
        asyncio.create_task(start_server(build_log_app(), "0.0.0.0", 8002)),
        asyncio.create_task(start_server(build_routing_app(), "0.0.0.0", 8003)),
        asyncio.create_task(start_server(build_config_app(cfg), "0.0.0.0", 8004)),
    ]
    await asyncio.sleep(1.0)

    registry = AgentRegistry()
    for agent_id, entry in cfg["agents"].items():
        await registry.register_from_card(agent_id, entry["card_url"], entry["endpoint"])
        card = registry.agents[agent_id].card
        skills = [s.get("id", "") for s in card.get("skills", [])]
        logger.info(
            "Registered agent=%s endpoint=%s skills=%s",
            agent_id,
            entry["endpoint"],
            ",".join(skills),
        )

    protocol_mode = str(cfg.get("a2a", {}).get("protocol_mode", "custom")).lower()
    if protocol_mode == "adk":
        router = ADKA2ARouter(registry, int(cfg["a2a"]["message_timeout_seconds"]))
    elif protocol_mode == "custom":
        router = A2ARouter(registry, int(cfg["a2a"]["message_timeout_seconds"]))
    else:
        raise ValueError("ConfigValidationError: a2a.protocol_mode must be either 'custom' or 'adk'")
    engine = NetCortexEngine(cfg, router)
    logger.info("Runtime initialized protocol_mode=%s", protocol_mode)
    return tasks, engine


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    content = "\n".join(json.dumps(row, default=str) for row in rows) + "\n"
    path.write_text(content, encoding="utf-8")


def write_outputs(report: dict[str, Any], incident_id: str, supervisor_state: dict[str, Any] | None = None):
    out_dir = Path("output") / incident_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rca_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    findings = report.get("agent_findings", [])
    trace_rows: list[dict[str, Any]] = []
    for finding in findings:
        trace_rows.append(
            {
                "timestamp": report.get("generated_at"),
                "agent": finding.get("agent_id"),
                "phase": "collaboration" if finding.get("revised") else "analysis",
                "action": "finding_update" if finding.get("revised") else "anomaly_detected",
                "detail": finding.get("summary", ""),
                "domain": finding.get("domain"),
                "anomaly_detected": finding.get("anomaly_detected"),
                "confidence": finding.get("confidence"),
                "revision_count": finding.get("revision_count", 0),
            }
        )
    _write_jsonl(out_dir / "agent_trace.jsonl", trace_rows)

    a2a_rows: list[dict[str, Any]] = []
    for msg in report.get("a2a_message_log", []):
        a2a_rows.append(
            {
                "message_id": msg.get("message_id"),
                "timestamp": msg.get("timestamp"),
                "sender_agent": msg.get("sender_agent"),
                "target_agent": msg.get("target_agent"),
                "message_type": msg.get("message_type"),
                "round_number": msg.get("round_number"),
                "status": msg.get("payload", {}).get("status", "unknown"),
                "payload": msg.get("payload", {}),
            }
        )
    _write_jsonl(out_dir / "a2a_messages.jsonl", a2a_rows)

    state = supervisor_state or {}
    state_summary = {
        "incident_id": incident_id,
        "degradation_type": state.get("degradation_type", "unknown"),
        "active_agents": state.get("active_agents", []),
        "collaboration_round": state.get("collaboration_round", 0),
        "collaboration_complete": state.get("collaboration_complete", False),
        "timed_out_agents": state.get("timed_out_agents", []),
        "findings_count": len(state.get("findings", [])),
        "revised_findings_count": len(state.get("revised_findings", [])),
        "a2a_messages_count": len(state.get("a2a_messages", [])),
        "generated_at": report.get("generated_at"),
    }
    (out_dir / "supervisor_state.json").write_text(json.dumps(state_summary, indent=2), encoding="utf-8")


def _keyword_coverage(report: dict[str, Any], expected_keywords: list[str]) -> tuple[list[str], list[str], float]:
    conflict_text = "conflict detected" if report.get("conflict_detected") else "no conflict"
    corpus_parts: list[str] = [
        str(report.get("root_cause", "")),
        str(report.get("human_readable_summary", "")),
        conflict_text,
    ]
    corpus_parts.extend(str(item) for item in report.get("contributing_factors", []))
    corpus_parts.extend(str(item) for item in report.get("causal_chain", []))
    corpus_parts.extend(str(item.get("summary", "")) for item in report.get("agent_findings", []))
    corpus = "\n".join(corpus_parts).lower()

    matched = [kw for kw in expected_keywords if kw.lower() in corpus]
    missing = [kw for kw in expected_keywords if kw.lower() not in corpus]
    coverage = 1.0 if not expected_keywords else len(matched) / len(expected_keywords)
    return matched, missing, coverage


@app.command()
def run(
    scenario: int = typer.Option(1),
    description: str = typer.Option("High error rate and throughput drop in us-east region"),
    config: str = typer.Option("config/config.yaml"),
    print_json: bool = typer.Option(False, help="Print full JSON report to console"),
    verbose: bool = typer.Option(False, help="Enable debug logging"),
    require_llm: bool = typer.Option(False, help="Fail the run if LLM-based classification/synthesis is unavailable"),
):
    async def _run():
        configure_runtime_logging(verbose)

        logger.info("Loading config from %s", config)
        cfg = load_config(config)
        cfg.setdefault("llm", {})["require_success"] = bool(require_llm)
        logger.info("LLM strict mode=%s for this run", bool(require_llm))
        tasks, engine = await start_runtime(cfg)

        incident = IncidentRequest(
            scenario_id=scenario,
            description=description,
            region=cfg["simulation"]["region"],
            severity="high",
            source_system="cli",
            external_incident_id=f"CLI-{scenario}",
        )
        logger.info(
            "Processing incident_id=%s scenario=%s region=%s description=%s",
            incident.incident_id,
            scenario,
            incident.region,
            incident.description,
        )
        report = await engine.run_incident(incident)
        report_data = report.model_dump(mode="json")
        write_outputs(report_data, incident.incident_id, engine.last_result_state)
        logger.info("Incident completed incident_id=%s", incident.incident_id)

        # Print human-readable summary first.
        print("\n" + "=" * 70)
        print("NetCortex RCA Report")
        print("=" * 70)
        print(f"Incident ID   : {report.incident_id}")
        print(f"Root Cause    : {report.root_cause}")
        print(f"Confidence    : {report.confidence_score:.0%}  "
              f"({report.corroborating_domain_count} corroborating domains)"
              + ("  [!] conflict detected" if report.conflict_detected else ""))
        if report.human_readable_summary:
            print("\nSummary:")
            print(report.human_readable_summary)
        print("\nCausal chain:")
        for step in report.causal_chain:
            print(f"  -> {step}")
        print("\nContributing factors:")
        for cf in report.contributing_factors:
            print(f"  * {cf}")
        print(f"\nFull report  : output/{incident.incident_id}/rca_report.json")
        print("Log files     : output/log/")
        print("=" * 70 + "\n")
        if print_json:
            print(json.dumps(report_data, indent=2))

        for t in tasks:
            t.cancel()
        # Suppress benign CancelledError from uvicorn lifespan shutdown.
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())
    raise SystemExit(0)


@app.command()
def serve(config: str = typer.Option("config/config.yaml")):
    async def _serve():
        cfg = load_config(config)
        tasks, engine = await start_runtime(cfg)

        webhook = WebhookServer()
        webhook.set_handler(engine.run_incident)
        ingest_task = asyncio.create_task(start_server(webhook.app, cfg["ingestion"]["host"], int(cfg["ingestion"]["port"])))

        try:
            await asyncio.gather(*tasks, ingest_task)
        except asyncio.CancelledError:
            pass

    asyncio.run(_serve())


@app.command()
def eval(
    all_scenarios: bool = typer.Option(False, "--all-scenarios", help="Run evaluation across all bundled scenarios"),
    scenario: int = typer.Option(1, help="Single scenario id to evaluate when --all-scenarios is not set"),
    config: str = typer.Option("config/config.yaml"),
    verbose: bool = typer.Option(False, help="Enable debug logging"),
    require_llm: bool = typer.Option(False, help="Fail the run if LLM-based classification/synthesis is unavailable"),
    fail_on_miss: bool = typer.Option(False, help="Return exit code 1 when any expected keyword is missing"),
):
    async def _run_eval():
        configure_runtime_logging(verbose)
        logger.info("Loading config from %s", config)
        cfg = load_config(config)
        cfg.setdefault("llm", {})["require_success"] = bool(require_llm)
        logger.info("LLM strict mode=%s for this eval", bool(require_llm))

        tasks, engine = await start_runtime(cfg)
        scenario_ids = sorted(SCENARIOS.keys()) if all_scenarios else [scenario]

        if not scenario_ids:
            raise typer.BadParameter("No scenarios available for evaluation")

        results: list[dict[str, Any]] = []
        eval_error: Exception | None = None
        try:
            for sid in scenario_ids:
                bundle = SCENARIOS.get(sid)
                if bundle is None:
                    raise typer.BadParameter(f"Unknown scenario id: {sid}")

                incident = IncidentRequest(
                    scenario_id=sid,
                    description=bundle.incident_request.description,
                    region=bundle.incident_request.region or cfg["simulation"]["region"],
                    severity=bundle.incident_request.severity,
                    source_system="eval",
                    external_incident_id=f"EVAL-{sid}",
                )

                logger.info("Evaluating scenario=%s name=%s incident_id=%s", sid, bundle.scenario_name, incident.incident_id)
                report = await engine.run_incident(incident)
                report_data = report.model_dump(mode="json")
                write_outputs(report_data, incident.incident_id, engine.last_result_state)

                matched, missing, coverage = _keyword_coverage(report_data, bundle.expected_rca_keywords)
                results.append(
                    {
                        "scenario_id": sid,
                        "scenario_name": bundle.scenario_name,
                        "incident_id": incident.incident_id,
                        "confidence": report.confidence_score,
                        "conflict_detected": report.conflict_detected,
                        "expected": len(bundle.expected_rca_keywords),
                        "matched": len(matched),
                        "coverage": coverage,
                        "missing_keywords": missing,
                    }
                )
        except Exception as exc:
            eval_error = exc
            logger.exception("Evaluation aborted after %s completed scenario(s)", len(results))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if not results and eval_error is not None:
            raise eval_error

        total_expected = sum(item["expected"] for item in results)
        total_matched = sum(item["matched"] for item in results)
        overall_coverage = 1.0 if total_expected == 0 else total_matched / total_expected

        print("\n" + "=" * 90)
        print("NetCortex Replay/Eval Report")
        print("=" * 90)
        for item in results:
            print(
                f"Scenario {item['scenario_id']:>2} | {item['scenario_name'][:42]:<42} "
                f"| keyword_coverage={item['matched']}/{item['expected']} ({item['coverage']:.0%}) "
                f"| confidence={item['confidence']:.0%} "
                f"| conflict={item['conflict_detected']}"
            )
            if item["missing_keywords"]:
                print(f"  missing: {', '.join(item['missing_keywords'])}")
            print(f"  output: output/{item['incident_id']}/rca_report.json")

        print("-" * 90)
        print(
            f"Overall keyword coverage: {total_matched}/{total_expected} ({overall_coverage:.0%}) "
            f"across {len(results)} scenario(s)"
        )
        print("=" * 90 + "\n")

        has_missing = any(bool(item["missing_keywords"]) for item in results)
        if fail_on_miss and has_missing:
            raise typer.Exit(code=1)
        if eval_error is not None:
            raise eval_error

    asyncio.run(_run_eval())
    raise SystemExit(0)


if __name__ == "__main__":
    app()
