"""Incident-text relevance filter (domains.json `relevance`, type incident_text).

Some domains are only worth reporting when the evidence relates to what
the incident describes — a config change to an unrelated component is
background noise. A hit on a signal listed in `applies_to` counts only if:

  * the incident description is empty or has no usable tokens, or
  * it shares a token with the signal's `text_fields`, or
  * the description mentions a `symptom_terms` word and the signal's
    `symptom_match.field` is one of `symptom_match.values`.

Tokens: lowercase alphanumeric runs of at least `min_token_length`, minus
`stopwords`. All configuration comes from the catalog.
"""

from __future__ import annotations

from typing import Any

from contract import Signal
from contract.catalog_models import RelevanceSpec
from detection.rules import field_value


def tokenize(text: str, min_len: int, stopwords: set[str]) -> set[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return {t for t in cleaned.split() if len(t) >= min_len and t not in stopwords}


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return "" if value is None else str(value)


def is_relevant(spec: RelevanceSpec, signal: Signal, incident_description: str) -> bool:
    if not incident_description.strip():
        return True
    stop = set(spec.stopwords)
    incident_tokens = tokenize(incident_description, spec.min_token_length, stop)
    if not incident_tokens:
        return True

    blob = " ".join(_flatten(field_value(signal, f)) for f in spec.text_fields)
    if incident_tokens & tokenize(blob, spec.min_token_length, stop):
        return True

    if spec.symptom_match is not None:
        description = incident_description.lower()
        if any(term in description for term in spec.symptom_terms):
            if str(field_value(signal, spec.symptom_match.field)) in spec.symptom_match.values:
                return True
    return False
