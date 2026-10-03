"""One correction for an agent turn that did not return a valid structured response.

Every built-in plugin asks its agents for structured responses through
:func:`structured_turn`, so a reply that cannot be parsed, or a provider that
gave up producing schema-valid output, gets the same treatment everywhere: one
follow-up turn in the same conversation that carries the validation errors.
A second failure raises :class:`~vs_runtime.api.StructuredResponseError`.
No caller fabricates a response in its place: a turn whose plugin records
failed attempts uses :func:`attempt_structured_turn` and records the
:class:`TurnFailed` outcome; any other turn lets the error end the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from vibesys.orchestration.prompts import render_template
from vs_runtime.api import AgentTurnTimeoutError, StructuredResponseError

if TYPE_CHECKING:
    from vs_runtime.api import AgentSession


async def structured_turn[ResponseT: BaseModel](
    session: AgentSession,
    message: str,
    response: type[ResponseT],
) -> ResponseT:
    """Run one turn, asking the same conversation once to re-emit an invalid reply.

    The follow-up keeps the agent's completed work and workspace edits;
    failing the turn instead would discard them. It raises
    ``StructuredResponseError`` if the corrected reply is invalid too.
    """
    try:
        return await session.turn(message, response=response)
    except StructuredResponseError as error:
        correction = render_template(
            "shared/structured_correction_prompt.j2", error=str(error), schema=response.__name__
        )
        return await session.turn(correction, response=response)


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """A turn that ended without a valid structured response.

    ``reason`` is the framework's account of the failure (the validation
    errors or the timeout), never text attributed to the agent.
    """

    reason: str


async def attempt_structured_turn[ResponseT: BaseModel](
    session: AgentSession,
    message: str,
    response: type[ResponseT],
) -> ResponseT | TurnFailed:
    """Run :func:`structured_turn`, reporting a reply that stayed invalid or timed out.

    For callers whose loop already records a failed attempt and retries.
    """
    try:
        return await structured_turn(session, message, response)
    except (StructuredResponseError, AgentTurnTimeoutError) as error:
        return TurnFailed(str(error))


__all__ = ["TurnFailed", "attempt_structured_turn", "structured_turn"]
