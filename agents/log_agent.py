"""The 'log' domain agent: the generic catalog-driven agent (agents/domain_agent.py).

Detection rules, summary text, confidence and the agent card all come from
contract/catalog/. This module only keeps the historical entry point.
"""

from __future__ import annotations

from fastapi import FastAPI

from agents.domain_agent import build_domain_app


def build_log_app(cfg: dict | None = None) -> FastAPI:
    return build_domain_app("log", cfg)
