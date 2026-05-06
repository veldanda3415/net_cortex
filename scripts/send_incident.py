from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import typer

app = typer.Typer(help="Send incidents to NetCortex webhook")


@app.command()
def send(
    url: str = typer.Option("http://localhost:8000/incidents"),
    scenario: int | None = typer.Option(None),
    file: str | None = typer.Option(None),
):
    if file:
        payload = json.loads(Path(file).read_text(encoding="utf-8"))
    else:
        payload = {
            "description": f"Simulation scenario {scenario or 1}",
            "region": "us-east",
            "severity": "high",
            "scenario_id": scenario or 1,
            "source_system": "script",
            "external_incident_id": f"SIM-{scenario or 1}",
            "reported_at": datetime.now(timezone.utc).isoformat(),
        }

    response = httpx.post(url, json=payload, timeout=30.0)
    response.raise_for_status()
    typer.echo(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    app()
