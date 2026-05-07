from __future__ import annotations

import json
import os

from models.schemas import IncidentRequest


def _heuristic_classify(incident: IncidentRequest) -> str:
    text = incident.description.lower()
    if "throughput" in text or "latency" in text or "packet" in text:
        return "network"
    if "config" in text or "policy" in text:
        return "config"
    if "overload" in text or "spike" in text:
        return "overload"
    return "unknown"


def _classify_with_llm(description: str, llm_model: str) -> str | None:
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError:
        return None

    api_key = os.environ.get("GEMINI_API_KEY", "")
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    gcp_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not api_key and not gcp_project:
        return None

    prompt = (
        "Classify incident degradation_type for orchestration.\n"
        "Valid labels: network, config, overload, unknown.\n"
        "Return JSON only: {\"degradation_type\": \"<label>\"}.\n\n"
        f"Incident description: {description}"
    )
    try:
        if api_key:
            client = genai.Client(api_key=api_key)
        else:
            client = genai.Client(vertexai=True, project=gcp_project, location=gcp_location)
        response = client.models.generate_content(
            model=llm_model,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        payload = json.loads(response.text)
        value = str(payload.get("degradation_type", "")).strip().lower()
        return value or None
    except Exception:
        return None

#just to add other agents for testing.
def classify_degradation(incident: IncidentRequest, llm_model: str, require_llm: bool) -> str:
    fallback = _heuristic_classify(incident)
    llm_value = _classify_with_llm(incident.description, llm_model)
    allowed = {"network", "config", "overload", "unknown"}
    if llm_value in allowed:
        # Keep routing/config coverage when incident symptoms look network-wide.
        if fallback == "network":
            return "network"
        # Let LLM refine only when heuristics are ambiguous.
        if fallback == "unknown":
            return llm_value
        # For explicit heuristic labels, do not widen/narrow based on LLM disagreements.
        return fallback
    if require_llm:
        raise RuntimeError("LLM incident classification is required but unavailable.")
    return fallback


# Mapping from degradation type to the minimal sufficient agent set.
# DESIGN NOTE: network degradation incidents always include all 4 domains because
# the incident description only states symptoms, not the cause. Config changes,
# routing changes, or hardware faults all surface as "high error rate / throughput drop".
# Safe pruning is only possible when the description explicitly names a domain (e.g.
# "after the config change" or "CPU overload spike").
_AGENT_SETS: dict[str, list[str]] = {
    # Network degradation symptoms: full scan required -- cause could be any domain.
    "network": ["metrics", "log", "routing", "config"],
    # Config-driven: user explicitly mentions a config/policy change.
    "config": ["config", "metrics"],
    # Overload: CPU/memory spike -- routing/config rarely relevant.
    "overload": ["metrics", "log"],
    # Unknown: full scan.
    "unknown": ["metrics", "log", "routing", "config"],
}


def select_active_agents(degradation_type: str) -> list[str]:
    return _AGENT_SETS.get(degradation_type, _AGENT_SETS["unknown"])
