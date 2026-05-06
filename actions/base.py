from __future__ import annotations

from abc import ABC, abstractmethod

from models.schemas import RCAReport


class ActionHandler(ABC):
    @abstractmethod
    async def handle(self, report: RCAReport) -> None:
        raise NotImplementedError
