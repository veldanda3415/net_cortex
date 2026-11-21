"""Shared pieces for the fine-tuning experiment: rebuild a drill's synthesizer
context from its saved record, render the exact prompt NetCortex sends the LLM,
and judge any model's JSON answer with NetCortex's own guards and evals/scoring.

Nothing here reads the faults log. expect.json (the per-fault cause/symptom
signal lists, as score.py uses) is only used offline to judge answers.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NETCORTEX_CATALOG_DIR", str(ROOT / "config" / "catalog.aws-lab"))

from agents.rca_synthesizer import (_build_llm_prompt, _cited_ok, _deterministic_from_cause,  # noqa: E402
                                    _evidence_index, _names, _rerank, _status_note)
from contract.catalog_loader import get_catalog  # noqa: E402
from evals.scoring import headline_names_culprit, load_expect, verdict  # noqa: E402
from models.schemas import A2AMessage, AgentFinding, RankedCause  # noqa: E402

CATALOG = get_catalog()
EXPECT = load_expect()
TARGET_KEYS = ["root_cause", "contributing_factors", "causal_chain", "metrics_affected", "human_readable_summary"]
CITE_ANY = re.compile(r"E\d+")


def _cid(c: dict) -> int:
    m = re.fullmatch(r"C(\d+)", str(c.get("id", "")))
    return int(m.group(1)) if m else 10**9


class Case:
    """One scored drill: everything the synthesizer saw, plus the judge's inputs."""

    def __init__(self, line: dict):
        self.line = line
        self.fault = line["fault"]
        self.round = line.get("round")
        rec = json.loads(Path(line["record"]).read_text())
        self.record = rec
        self.report = rec["report"]
        self.incident = rec["incident"]
        self.findings = [AgentFinding.model_validate(f) for f in self.report.get("agent_findings", [])]
        self.messages = [A2AMessage.model_validate(m) for m in self.report.get("a2a_message_log", [])]
        # C1..Cn are numbered in the rules order, before the LLM's re-rank: the prompt's order
        self.ranked = [RankedCause.model_validate(c) for c in sorted(self.report.get("ranked_causes") or [], key=_cid)]
        self.status = self.report.get("incident_status", "unknown")
        self.expect = EXPECT[self.fault]
        self.verdict, self.cause, _ = verdict(self.expect, self.report)
        self.id = f"{self.round}:{self.fault}:{self.report.get('incident_id', '')[:8]}"

    # ---- prompt -------------------------------------------------------------
    def prompt(self, order: list[int] | None = None) -> tuple[str, dict[str, str]]:
        """The synthesizer prompt; `order` permutes the agent findings (agents
        finish in any order), which renumbers the evidence ids naturally.
        Returns (prompt, {original evidence id -> id in this prompt})."""
        findings = self.findings if order is None else [self.findings[i] for i in order]
        evidence = _evidence_index(findings)
        base = _evidence_index(self.findings)
        by_text: dict[str, list[str]] = {}
        for k, v in evidence.items():
            by_text.setdefault(v, []).append(k)
        mapping = {k: by_text[v].pop(0) for k, v in base.items()}
        text = _build_llm_prompt(self.incident.get("description", ""), findings, self.messages, CATALOG,
                                 self.ranked, evidence, self.status)
        return text, mapping

    def permutations(self, n: int, seed: int = 0) -> list[list[int]]:
        idx = list(range(len(self.findings)))
        rng = random.Random(f"{self.id}:{seed}")
        seen, out = {tuple(idx)}, []
        for _ in range(50 * n):
            if len(out) >= n:
                break
            p = idx[:]
            rng.shuffle(p)
            if tuple(p) not in seen:
                seen.add(tuple(p))
                out.append(p)
        return out

    # ---- judge --------------------------------------------------------------
    def judge(self, answer: dict | None) -> dict:
        """NetCortex's guards plus the drill scorer, on one model answer
        (evidence ids as in the unpermuted prompt)."""
        evidence = _evidence_index(self.findings)
        ok_json = isinstance(answer, dict) and isinstance(answer.get("root_cause"), str)
        root = answer["root_cause"] if ok_json else ""
        cited = ok_json and _cited_ok(root, evidence)
        ranked = _rerank(self.ranked, answer if ok_json else None)
        names_top = ok_json and (not ranked or _names(root, ranked[0]))
        accepted = bool(ok_json and cited and names_top)
        det = (_deterministic_from_cause(ranked[0], self.findings, self.incident.get("alarm_entities") or [])
               + _status_note(self.status)) if ranked else (self.report.get("root_cause") or "")
        final = root if accepted else det
        culprit_case = self.verdict == "cause"
        return {
            "json_ok": ok_json,
            "cited_ok": bool(cited),
            "names_top": bool(names_top),
            "accepted": accepted,
            "culprit_case": culprit_case,
            "headline_raw": culprit_case and headline_names_culprit(self.expect, {**self.report, "root_cause": root}, self.cause),
            "headline_final": culprit_case and headline_names_culprit(self.expect, {**self.report, "root_cause": final}, self.cause),
        }


def remap_citations(answer: dict, mapping: dict[str, str]) -> dict:
    """Rewrite E<n> ids in every string of an answer through `mapping`."""
    def fix(s):
        return CITE_ANY.sub(lambda m: mapping.get(m.group(0), m.group(0)), s) if isinstance(s, str) else s
    out = {}
    for k, v in answer.items():
        out[k] = [fix(x) for x in v] if isinstance(v, list) else fix(v)
    return out


def load_cases(paths: list[str]) -> list[Case]:
    cases = []
    for p in paths:
        for raw in Path(p).read_text().splitlines():
            if not raw.strip():
                continue
            line = json.loads(raw)
            if line.get("record") and (line.get("netcortex") or {}).get("verdict") not in (None, "stale"):
                cases.append(Case(line))
    return cases


def parse_json(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        v = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            v = json.loads(m.group(0))
        except Exception:
            return None
    if isinstance(v, list) and len(v) == 1:
        v = v[0]
    return v if isinstance(v, dict) else None
