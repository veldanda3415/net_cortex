from __future__ import annotations

from actions.base import ActionHandler
from models.schemas import RCAReport


class Notifier(ActionHandler):
    async def handle(self, report: RCAReport) -> None:
        # Stub for chat/email notifications.
        _ = report
