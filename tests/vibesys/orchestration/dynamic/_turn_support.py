"""Explicit provider-turn barriers shared by dynamic lifecycle tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.agents import IMPLEMENTER

if TYPE_CHECKING:
    from pydantic import BaseModel
    from tests.vibesys.orchestration.dynamic._support import Script

    from vs_runtime.api import AgentRole


class HeldTurns:
    """Hold every implementer turn open until released, as a long provider turn is."""

    def __init__(self, script: Script, *, role: AgentRole = IMPLEMENTER) -> None:
        self.script = script
        self.role = role
        self.opened: asyncio.Queue[int] = asyncio.Queue()
        self.release = asyncio.Event()
        self._turns = 0

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        reply = self.script.respond(role, history, message, response)
        if role.id != self.role.id:
            return reply
        self._turns += 1
        self.opened.put_nowait(self._turns)

        async def held() -> object:
            await self.release.wait()
            return reply

        return held()


__all__ = ["HeldTurns"]
