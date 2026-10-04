"""Correct typed semantic agent errors before accepting a completed role turn."""

from __future__ import annotations

from typing import TYPE_CHECKING, overload

from pydantic import BaseModel

from vibesys.orchestration.structured_turn import structured_turn
from vibesys.prompts import render_template
from vs_evaluation.api import EvaluationAgentAccessError
from vs_runtime.api import StructuredResponseError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_agent.api import AgentSessionKey, InvocationOutcome
    from vs_prompts.api import RenderedPrompt
    from vs_runtime.api import AgentConversation, AgentRole, Workspace


async def validated_turn[ResponseT: BaseModel](
    session: AgentConversation,
    message: str,
    response: type[ResponseT],
    *,
    invocation_id: str | None = None,
    validate_response: Callable[[ResponseT], Awaitable[None]],
) -> ResponseT:
    """Validate completed replies, allowing one same-conversation semantic correction."""
    invocation_id = invocation_id or session.invocation_id
    result = await structured_turn(session, message, response, invocation_id=invocation_id)
    try:
        await validate_response(result)
    except (EvaluationAgentAccessError, StructuredResponseError) as error:
        correction = render_template(
            "shared/structured_correction_prompt.j2", error=str(error), schema=response.__name__
        )
        result = await structured_turn(
            session,
            correction,
            response,
            invocation_id=None if invocation_id is None else f"{invocation_id}/semantic-correction",
        )
        await validate_response(result)
    return result


__all__ = ["validated_turn"]


class _ValidatedConversation:
    """Delegate conversation ownership while validating completed structured turns."""

    def __init__[ResponseT: BaseModel](
        self,
        session: AgentConversation,
        response: type[ResponseT],
        validate: Callable[[ResponseT], Awaitable[None]],
    ) -> None:
        self._session = session

        async def checked(value: BaseModel) -> None:
            await validate(response.model_validate(value))

        self._validate = checked

    @property
    def role(self) -> AgentRole:
        return self._session.role

    @property
    def workspace(self) -> Workspace:
        return self._session.workspace

    @property
    def member_id(self) -> str | None:
        return self._session.member_id

    @property
    def closed(self) -> bool:
        return self._session.closed

    @property
    def session_key(self) -> AgentSessionKey:
        return self._session.session_key

    @property
    def invocation_id(self) -> str | None:
        return self._session.invocation_id

    def inspect(self, invocation_id: str) -> InvocationOutcome:
        return self._session.inspect(invocation_id)

    async def resume(
        self,
        message: RenderedPrompt,
        invocation_id: str,
        *,
        response: type[BaseModel] | None = None,
    ) -> InvocationOutcome:
        return await self._session.resume(message, invocation_id, response=response)

    @overload
    async def turn(
        self, message: str, *, response: None = None, invocation_id: str | None = None
    ) -> str: ...

    @overload
    async def turn[ResponseT: BaseModel](
        self, message: str, *, response: type[ResponseT], invocation_id: str | None = None
    ) -> ResponseT: ...

    async def turn[ResponseT: BaseModel](
        self,
        message: str,
        *,
        response: type[ResponseT] | None = None,
        invocation_id: str | None = None,
    ) -> str | ResponseT:
        if response is None:
            return await self._session.turn(message, invocation_id=invocation_id)
        result = await self._session.turn(message, response=response, invocation_id=invocation_id)
        try:
            await self._validate(result)
        except EvaluationAgentAccessError as error:
            raise StructuredResponseError(self.role.id, response, detail=str(error)) from error
        return result

    async def close(self) -> None:
        await self._session.close()


def validated_conversation[ResponseT: BaseModel](
    session: AgentConversation,
    response: type[ResponseT],
    validate: Callable[[ResponseT], Awaitable[None]],
) -> AgentConversation:
    """Wrap structured turn acceptance, preserving the underlying conversation authority."""
    return _ValidatedConversation(session, response, validate)


__all__ += ["validated_conversation"]
