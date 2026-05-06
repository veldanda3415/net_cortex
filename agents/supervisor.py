from __future__ import annotations

from models.schemas import IncidentRequest


def classify_degradation(incident: IncidentRequest) -> str:
    text = incident.description.lower()
    if "throughput" in text or "latency" in text or "packet" in text:
        return "network"
    if "config" in text or "policy" in text:
        return "config"
    if "overload" in text or "spike" in text:
        return "overload"
    return "unknown"


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
