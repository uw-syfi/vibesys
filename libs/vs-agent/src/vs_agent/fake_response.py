"""Injectable structured-response scenarios for agent test doubles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class AgentResponseContext:
    """Generic turn information a response scenario may use."""

    role: str
    output_schema: type[BaseModel] | None
    round_label: str | None
    turn_number: int


type AgentResponse = BaseModel | Mapping[str, object]


class AgentResponseScenario(Protocol):
    """Produce optional structured answers for deterministic agent turns."""

    def respond(self, context: AgentResponseContext) -> AgentResponse | None:
        """Return an answer for ``context``, or ``None`` to use the fallback."""
