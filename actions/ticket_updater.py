from __future__ import annotations

from actions.base import ActionHandler
from models.schemas import RCAReport


class TicketUpdater(ActionHandler):
    async def handle(self, report: RCAReport) -> None:
        # Stub for external ticketing integration.
        _ = report
