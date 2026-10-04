"""Session state, dispatch, and isolation for the public runtime Fake.

The fake factory supplies a narrow workspace role. Conversation history and
journal ownership stay here, independently of workspace composition.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypeVar, overload

from pydantic import BaseModel, ValidationError

from vs_agent.api import (
    AgentOutputSchemaError,
    AgentSessionCheckpoint,
    AgentSessionKey,
    SessionConfigurationError,
    describe_validation_error,
)
from vs_runtime._agent_sessions import await_session_operation
from vs_runtime._fake_agent_invocations import FakeAgentInvocations, FakeInvocationIdentity
from vs_runtime._workspace_access import WorkspaceAccessRecovery, WorkspaceAccessTarget
from vs_runtime.contracts import (
    AgentBinding,
    AgentRole,
    SessionClosedError,
    SessionTransportUnavailableError,
    StructuredResponseError,
    Workspace,
    WorkspaceAccess,
)

if TYPE_CHECKING:
    from vs_agent.api import AgentInvocationStore, AgentSessions, InvocationOutcome
    from vs_prompts.api import RenderedPrompt

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class _SessionWorkspace(Workspace, WorkspaceAccessTarget, Protocol):
    """Workspace revision and access recovery effects owned by a fake session."""

    @property
    def access_recovery(self) -> WorkspaceAccessRecovery:
        """Return the workspace-owned pending isolation recovery."""
        ...


# A responder may return an awaitable: the turn awaits it, so a test can hold a
# turn open the way a long provider turn is, and end it early.
TurnResponder: TypeAlias = Callable[
    [AgentRole, tuple[str, ...], str, type[BaseModel] | None], object
]


@dataclass(frozen=True)
class FakeSessionConfiguration:
    """Immutable creation options shared by a fake session."""

    member_id: str | None
    session_key: AgentSessionKey
    writable_paths: tuple[str, ...]
    writable_directory_paths: tuple[str, ...]
    #: The resumed conversation's history, shared with earlier sessions.
    history: list[str] | None = None
    session_transport: AgentSessions | None = None
    invocation_store: AgentInvocationStore | None = None


def echo_responder(
    _role: AgentRole,
    _history: tuple[str, ...],
    message: str,
    _response: type[BaseModel] | None,
) -> object:
    """Return the supplied message for an unscripted text conversation."""
    return message


class FakeAgentSession:
    """In-memory conversation with the public lifetime and context contract."""

    def __init__(
        self,
        role: AgentRole,
        workspace: _SessionWorkspace,
        binding: AgentBinding,
        responder: TurnResponder,
        config: FakeSessionConfiguration,
    ) -> None:
        """Bind a session to its configuration and, if resumed, its conversation."""
        self._session_transport = config.session_transport
        self._session_key = config.session_key
        self._initial_invocations = FakeAgentInvocations(
            FakeInvocationIdentity(
                self._session_key,
                role.model_dump_json(),
                str(workspace.path),
                config.writable_paths,
            ),
            config.invocation_store,
            config.session_transport,
        )
        self._role = role
        self._workspace = workspace
        self._member_id = config.member_id
        self._writable_paths = config.writable_paths
        self._writable_directory_paths = config.writable_directory_paths
        self._binding = binding
        self._responder = responder
        self._history: list[str] = [] if config.history is None else config.history
        self._turn_number = 0
        self._turn_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def role(self) -> AgentRole:
        """Return the role bound at creation."""
        return self._role

    @property
    def workspace(self) -> Workspace:
        """Return the workspace bound at creation."""
        return self._workspace

    @property
    def member_id(self) -> str | None:
        """Return the durable policy identity for this instance."""
        return self._member_id

    @property
    def writable_paths(self) -> tuple[str, ...]:
        """Return the immutable session-specific write grants."""
        return self._writable_paths

    @property
    def binding(self) -> AgentBinding:
        """Return the configured immutable runtime attribution."""
        return self._binding

    @property
    def closed(self) -> bool:
        """Return whether cleanup has ended this session."""
        return self._closed

    @property
    def history(self) -> tuple[str, ...]:
        """Return completed user messages in conversation order."""
        return tuple(self._history)

    @property
    def retains_session_key(self) -> bool:
        """Retain exclusive ownership through pending or failed cleanup."""
        task = self._close_task
        return task is None or not task.done() or task.cancelled() or task.exception() is not None

    @property
    def session_key(self) -> AgentSessionKey:
        """Return the same identity production binds for member sessions."""
        return self._session_key

    @property
    def invocation_id(self) -> str | None:
        """Return no default identity for an initialized, unbound session."""
        return None

    def checkpoint(self) -> AgentSessionCheckpoint:
        """Read the exact provider identity represented by durable invocation evidence."""
        try:
            return self._initial_invocations.checkpoint()
        except SessionConfigurationError as error:
            raise SessionTransportUnavailableError(str(error)) from error

    def release_interrupted(self, invocation_id: str) -> None:
        """Release original and correction fences after the caller drains the turn."""
        self._initial_invocations.release_interrupted(
            invocation_id,
            active=self._turn_lock.locked(),
        )

    def inspect(self, invocation_id: str) -> InvocationOutcome:
        """Read durable initial evidence or inspect the configured continuation transport."""
        if not self._session_key.durable:
            self.checkpoint()
        return self._initial_invocations.inspect(invocation_id)

    async def resume(
        self,
        message: RenderedPrompt,
        invocation_id: str,
        *,
        response: type[BaseModel] | None = None,
    ) -> InvocationOutcome:
        """Resume with production-equivalent workspace isolation."""
        del response
        if self._closed:
            raise SessionClosedError
        async with self._turn_lock:
            if self._closed:
                raise SessionClosedError
            transport = self._session_transport
            if transport is None:
                detail = "durable agent session transport is not configured"
                raise SessionTransportUnavailableError(detail)
            revision = await self._workspace.snapshot("session-resume-input")
            try:
                outcome = await await_session_operation(
                    asyncio.create_task(
                        asyncio.to_thread(
                            transport.resume, self._session_key, message, invocation_id
                        )
                    )
                )
            finally:
                await await_session_operation(
                    asyncio.create_task(self._enforce_workspace_access(revision))
                )
            if (
                self._role.workspace_access is WorkspaceAccess.READ_WRITE
                or await self._workspace.pending_changes()
            ):
                await self._workspace.snapshot("session-resume")
            return outcome

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
        """Serialize turns, respond from completed history, and enforce access."""
        if self._closed:
            raise SessionClosedError
        async with self._turn_lock:
            if self._closed:
                raise SessionClosedError
            if invocation_id is None:
                return await self._turn_once(message, response=response)
            return await self._journal_turn(message, response, invocation_id)

    async def _journal_turn(
        self,
        message: str,
        response: type[ResponseT] | None,
        invocation_id: str,
    ) -> str | ResponseT:
        outcome = self._initial_invocations.begin(
            message,
            None if response is None else response.model_json_schema(),
            invocation_id,
        )
        try:
            if outcome is not None:
                return self._initial_invocations.replay(outcome, response)
            return await self._turn_once(
                message,
                response=response,
                on_response=lambda text: self._initial_invocations.accepted(invocation_id, text),
            )
        except AgentOutputSchemaError as error:
            if response is None:
                raise
            raise StructuredResponseError(self._role.id, response, detail=error.detail) from error
        except StructuredResponseError as error:
            self._initial_invocations.rejected(invocation_id, error.detail)
            raise
        finally:
            self._initial_invocations.end(invocation_id)

    async def _turn_once(
        self,
        message: str,
        *,
        response: type[ResponseT] | None,
        on_response: Callable[[str], None] | None = None,
    ) -> str | ResponseT:
        self._turn_number += 1
        label = f"{self._role.id}-session-turn-{self._turn_number}"
        revision = await self._workspace.snapshot(f"{label}-input")
        try:
            result = await self._respond(message, response, on_response)
        except StructuredResponseError:
            # Production keeps the conversation after an invalid structured
            # reply, so the correction turn sees this message in its history.
            self._history.append(message)
            raise
        finally:
            remaining_changes = await self._enforce_workspace_access(revision)
        self._history.append(message)
        if self._role.workspace_access is WorkspaceAccess.READ_WRITE or remaining_changes:
            await self._workspace.snapshot(label)
        return result

    async def _respond(
        self,
        message: str,
        response: type[ResponseT] | None,
        on_response: Callable[[str], None] | None = None,
    ) -> str | ResponseT:
        """Answer one turn, reporting invalid structured output as production does."""
        try:
            value = self._responder(self._role, tuple(self._history), message, response)
            if inspect.isawaitable(value):
                value = await value
        except AgentOutputSchemaError as error:
            if response is None:
                raise
            raise StructuredResponseError(self._role.id, response, detail=error.detail) from error
        if response is None:
            if not isinstance(value, str):
                error = "text turn responder must return str"
                raise TypeError(error)
            if on_response is not None:
                on_response(value)
            return value
        if on_response is not None:
            on_response(
                value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
            )
        try:
            return response.model_validate(value)
        except ValidationError as error:
            raise StructuredResponseError(
                self._role.id, response, detail=describe_validation_error(error)
            ) from error

    async def _enforce_workspace_access(self, revision: str) -> list[str]:
        if self._role.workspace_access is not WorkspaceAccess.READ_WRITE:
            limited = self._role.workspace_access is WorkspaceAccess.LIMITED
            self._workspace.access_recovery.begin(
                revision,
                self._role.id,
                self._writable_paths if limited else (),
                self._writable_directory_paths if limited else (),
            )
        if self._role.workspace_access is WorkspaceAccess.READ_WRITE:
            return await self._workspace.pending_changes()
        result = await self._workspace.access_recovery.reconcile(self._workspace)
        return result.pending_changes

    async def close(self) -> None:
        """Reject more work and wait for the active turn before closing."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        async with self._turn_lock:
            return

    def mark_closed(self) -> None:
        """Reject new and already-queued turns before owner cleanup starts."""
        self._closed = True


__all__ = ["FakeAgentSession", "FakeSessionConfiguration", "TurnResponder", "echo_responder"]
