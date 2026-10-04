"""Runtime ownership of deferred conversation setup, dispatch and cancellation."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, TypeVar, overload

from pydantic import BaseModel, ValidationError

from vs_agent.api import Completed, InvalidResponse, InvocationConflictError
from vs_runtime._agent_declarations import agent_session_key
from vs_runtime.contracts import (
    AgentConversationOpenError,
    AgentConversationRequest,
    InvocationRelease,
    RuntimeContractError,
    SessionClosedError,
    StructuredResponseError,
    validate_member_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_agent.api import AgentSessionKey, InvocationOutcome
    from vs_prompts.api import RenderedPrompt
    from vs_runtime.contracts import (
        AgentRole,
        AgentSession,
        PreparedConversation,
        Workspace,
        WorkspaceAgentSessions,
    )

ResponseT = TypeVar("ResponseT", bound=BaseModel)


async def _drain(task: asyncio.Task[object]) -> None:
    settled = asyncio.gather(task, return_exceptions=True)
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            continue


class RuntimePreparedConversation:
    """Own one immutable binding without claiming initialized session attribution."""

    def __init__(
        self,
        owner: WorkspaceAgentSessions,
        request: AgentConversationRequest,
        inspect: Callable[[AgentSessionKey, str], InvocationOutcome],
    ) -> None:
        if request.member_id is None:
            detail = "prepared conversation requires a durable member_id"
            raise RuntimeContractError(detail)
        validate_member_id(request.member_id)
        self._owner = owner
        self._request = request
        self._inspect = inspect
        self._key = agent_session_key(
            request.role.id, request.member_id, request.generation, uuid.uuid4().hex
        )
        self._session: AgentSession | None = None
        self._operation: asyncio.Task[object] | None = None
        self._opening = False
        self._released = False
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._cancelled_invocations: set[str] = set()
        self._release: InvocationRelease | None = None

    @property
    def role(self) -> AgentRole:
        return self._request.role

    @property
    def workspace(self) -> Workspace:
        return self._request.workspace

    @property
    def member_id(self) -> str | None:
        return self._request.member_id

    @property
    def closed(self) -> bool:
        return self._closed or (self._session is not None and self._session.closed)

    @property
    def session_key(self) -> AgentSessionKey:
        return self._key if self._session is None else self._session.session_key

    @property
    def invocation_id(self) -> str | None:
        return self._request.invocation_id

    def inspect(self, invocation_id: str) -> InvocationOutcome:
        if self._session is not None and not self._session.closed:
            return self._session.inspect(invocation_id)
        return self._inspect(self._key, invocation_id)

    def authorize_release(self, authority: InvocationRelease) -> None:
        if self.closed:
            raise SessionClosedError
        if self._release is not None and self._release != authority:
            detail = "conversation release authority changed"
            raise InvocationConflictError.because(detail)
        self._release = authority
        self._apply_release()

    def _apply_release(self) -> None:
        authority = self._release
        if (
            authority is not None
            and bool(
                {authority.invocation_id, f"{authority.invocation_id}/correction"}
                & self._cancelled_invocations
            )
            and self._session is not None
            and not self._released
        ):
            outcome = self._session.inspect(authority.invocation_id)
            correction_cancelled = (
                f"{authority.invocation_id}/correction" in self._cancelled_invocations
            )
            if not isinstance(outcome, Completed) or correction_cancelled:
                self._session.release_interrupted(authority.invocation_id)
            self._released = True

    async def _open(self) -> AgentSession:
        if self._session is None:
            request = self._request
            try:
                self._session = await self._owner.create_session(
                    request.role,
                    workspace=request.workspace,
                    member_id=request.member_id,
                    generation=request.generation,
                    writable_paths=request.writable_paths,
                )
            except Exception as error:
                raise AgentConversationOpenError(str(error)) from error
        return self._session

    async def _run[Result](
        self, invocation_id: str | None, operation: Callable[[AgentSession], Awaitable[Result]]
    ) -> Result:
        async with self._lock:
            if self.closed:
                raise SessionClosedError
            # Opening is drained without cancellation so a successful resource
            # acquisition is always assigned to this owner before cleanup.
            opening = asyncio.create_task(self._open())
            self._operation = opening
            self._opening = True
            try:
                session = await asyncio.shield(opening)
            except asyncio.CancelledError:
                await _drain(opening)
                raise
            finally:
                self._operation = None
                self._opening = False
            if self.closed:
                raise SessionClosedError

            async def dispatch() -> Result:
                return await operation(session)

            task = asyncio.create_task(dispatch())
            self._operation = task
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                task.cancel()
                await _drain(task)
                if task.cancelled() and invocation_id is not None:
                    self._cancelled_invocations.add(invocation_id)
                self._apply_release()
                raise
            finally:
                self._operation = None

    @overload
    async def turn(
        self, message: str, *, response: None = None, invocation_id: str | None = None
    ) -> str: ...

    @overload
    async def turn(
        self, message: str, *, response: type[ResponseT], invocation_id: str | None = None
    ) -> ResponseT: ...

    async def turn(
        self,
        message: str,
        *,
        response: type[ResponseT] | None = None,
        invocation_id: str | None = None,
    ) -> str | ResponseT:
        if self.closed:
            raise SessionClosedError
        bound = self._request.invocation_id
        if (
            bound is not None
            and invocation_id is not None
            and invocation_id not in {bound, f"{bound}/correction"}
        ):
            detail = "bound invocation identity changed"
            raise InvocationConflictError.because(detail)
        identity = invocation_id or bound
        if identity is not None:
            outcome = self.inspect(identity)
            if isinstance(outcome, InvalidResponse) and response is not None:
                raise StructuredResponseError(self.role.id, response, detail=outcome.detail)
            if isinstance(outcome, Completed):
                if response is None:
                    return outcome.result.text
                try:
                    return response.model_validate_json(outcome.result.text)
                except ValidationError as error:
                    raise StructuredResponseError(
                        self.role.id, response, detail=str(error)
                    ) from error
        return await self._run(
            identity,
            lambda session: session.turn(message, response=response, invocation_id=identity),
        )

    async def resume(
        self,
        message: RenderedPrompt,
        invocation_id: str,
        *,
        response: type[BaseModel] | None = None,
    ) -> InvocationOutcome:
        return await self._run(
            invocation_id,
            lambda session: session.resume(message, invocation_id, response=response),
        )

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_once())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await _drain(self._close_task)
            raise

    async def _close_once(self) -> None:
        operation = self._operation
        if operation is not None:
            if not self._opening:
                operation.cancel()
            await _drain(operation)
        async with self._lock:
            session = self._session
            if session is not None:
                try:
                    self._apply_release()
                finally:
                    await session.close()


def prepare_agent_conversation(
    owner: WorkspaceAgentSessions, request: AgentConversationRequest
) -> PreparedConversation:
    """Bind a durable conversation through the owning factory's public effects.

    Factory decorators use this composition seam to preserve creation and
    inspection authority rather than bypassing their own effect boundaries.
    """
    return RuntimePreparedConversation(owner, request, owner.inspect_invocation)


__all__ = ["RuntimePreparedConversation", "prepare_agent_conversation"]
