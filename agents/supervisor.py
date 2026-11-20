from __future__ import annotations

from agents import llm_client
from contract.catalog_loader import Catalog, get_catalog
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
    """LLM label for the incident, via agents/llm_client.py (Gemini or Bedrock per the
    model reference). None when no LLM is configured or the call fails."""
    if not llm_client.available(llm_model):
        return None
    prompt = (
        "Classify incident degradation_type for orchestration.\n"
        f"Valid labels: {', '.join(get_catalog().domains.degradation_map)}.\n"
        "Return JSON only: {\"degradation_type\": \"<label>\"}.\n\n"
        f"Incident description: {description}"
    )
    payload = llm_client.generate_json(prompt, llm_model)
    if not payload:
        return None
    value = str(payload.get("degradation_type", "")).strip().lower()
    return value or None


def classify_degradation(incident: IncidentRequest, llm_model: str, require_llm: bool) -> str:
    fallback = _heuristic_classify(incident)
    llm_value = _classify_with_llm(incident.description, llm_model)
    allowed = set(get_catalog().domains.degradation_map)
    if llm_value in allowed:
        return llm_value
    if require_llm:
        raise RuntimeError("LLM incident classification is required but unavailable.")
    return fallback


# The degradation type -> minimal sufficient domain set mapping lives in the
# catalog: contract/catalog/domains.json `degradation_map`.
# DESIGN NOTE: network degradation incidents always include every domain because
# the incident description only states symptoms, not the cause. Config changes,
# routing changes, or hardware faults all surface as "high error rate / throughput drop".
# Safe pruning is only possible when the description explicitly names a domain (e.g.
# "after the config change" or "CPU overload spike").
def select_active_agents(degradation_type: str, catalog: Catalog | None = None) -> list[str]:
    """Domains to run for a degradation type, from domains.json `degradation_map`
    (falls back to the map's `unknown` entry)."""
    return (catalog or get_catalog()).active_domains(degradation_type)
