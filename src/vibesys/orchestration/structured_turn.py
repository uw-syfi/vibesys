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

from vibesys.prompts import render_template
from vs_evaluation.api import EvaluationAgentAccessError
from vs_runtime.api import (
    AgentTurnTimeoutError,
    StructuredResponseError,
    bind_agent_invocation,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_runtime.api import AgentConversation


async def structured_turn[ResponseT: BaseModel](
    session: AgentConversation,
    message: str,
    response: type[ResponseT],
    *,
    invocation_id: str | None = None,
    validate_response: Callable[[ResponseT], Awaitable[None]] | None = None,
) -> ResponseT:
    """Run one turn, asking the same conversation once to re-emit an invalid reply.

    The follow-up keeps the agent's completed work and workspace edits;
    failing the turn instead would discard them. It raises
    ``StructuredResponseError`` if the corrected reply is invalid too.
    """
    invocation_id = invocation_id or session.invocation_id
    original_session = session
    session = bind_agent_invocation(original_session, invocation_id)
    try:
        result = await session.turn(message, response=response)
        if validate_response is not None:
            await validate_response(result)
    except (StructuredResponseError, EvaluationAgentAccessError) as error:
        correction = render_template(
            "shared/structured_correction_prompt.j2", error=str(error), schema=response.__name__
        )
        session = bind_agent_invocation(
            original_session, None if invocation_id is None else f"{invocation_id}/correction"
        )
        result = await session.turn(correction, response=response)
        if validate_response is not None:
            await validate_response(result)
        return result
    else:
        return result


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """A turn that ended without a valid structured response.

    ``reason`` is the framework's account of the failure (the validation
    errors or the timeout), never text attributed to the agent.
    """

    reason: str


async def attempt_structured_turn[ResponseT: BaseModel](
    session: AgentConversation,
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
