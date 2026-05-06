from __future__ import annotations

from actions.base import ActionHandler
from models.schemas import RCAReport


class RemediationTrigger(ActionHandler):
    async def handle(self, report: RCAReport) -> None:
        # Stub for runbook automation hooks.
        _ = report
