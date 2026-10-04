"""Runtime-backed provision for delegated profiler-agent conversations."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from vibesys.orchestration.structured_turn import structured_turn
from vibesys.prompts import render_template
from vs_evaluation.api import (
    EvaluationAgentAccessError,
    EvaluationAgentRole,
    EvaluationPending,
    EvaluationUnknown,
    OwnedEvaluationDependencies,
    ProfilerAgentResult,
    evaluation_principal,
)
from vs_runtime.api import AgentCapability, Completed, RunCleanupError, RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_evaluation.api import EvaluationBackend, EvaluationSettlements
    from vs_runtime.api import (
        AgentRole,
        AgentSession,
        CandidateWorkspace,
        WorkspaceAgentSessions,
        Workspaces,
    )


_CLEANUP_FAILURE = "profiler conversation cleanup failed"


class _WaitingForEvaluation(BaseModel):
    """Profiler wire reply yielding owned captures to host settlement."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["waiting_for_evaluation"]
    handles: tuple[str, ...] = Field(min_length=1)

    @field_validator("handles")
    @classmethod
    def unique_handles(cls, handles: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(handles)) != len(handles) or any(
            not handle.strip() or handle != handle.strip() for handle in handles
        ):
            message = "evaluation handles must be nonblank, trimmed and unique"
            raise ValueError(message)
        return handles


_ProfilerReply = RootModel[ProfilerAgentResult | _WaitingForEvaluation]


@dataclass(slots=True)
class _Conversation:
    scope_id: str | None
    workspace: CandidateWorkspace
    session: AgentSession


@dataclass(frozen=True, slots=True)
class ProfilerEvaluationAccess:
    """Scoped requester metadata and host settlement ports for profiler continuations."""

    backend: EvaluationBackend
    settlements: EvaluationSettlements
    requester_generation: Callable[[str, str], Awaitable[int]]
    validate_wait: Callable[..., Awaitable[None]]
    cancel_associations: Callable[[str], Awaitable[None]]


class RuntimeProfilerTurnProvision:
    """Create resumable profiler sessions through public runtime capabilities."""

    def __init__(
        self,
        role: AgentRole,
        agents: WorkspaceAgentSessions,
        workspaces: Workspaces,
        *,
        evaluation: ProfilerEvaluationAccess | None = None,
    ) -> None:
        """Bind the profiler role to run-owned agent and workspace capabilities."""
        if AgentCapability.DURABLE_TURN_CONTINUATION in role.required_capabilities and (
            evaluation is None
        ):
            message = "suspending profiler requires evaluation and settlement capabilities"
            raise RuntimeContractError(message)
        self._evaluation = evaluation
        self._role = role
        self._agents = agents
        self._workspaces = workspaces
        self._conversations: dict[str, _Conversation] = {}
        self._operations: dict[str, asyncio.Task[ProfilerAgentResult]] = {}
        self._lock = asyncio.Lock()
        declaration = role.model_dump_json().encode()
        self._identity = hashlib.sha256(declaration).hexdigest()

    @property
    def identity(self) -> str:
        """Return a stable identity for the profiler role declaration."""
        return self._identity

    async def run_turn(
        self,
        *,
        session_id: str,
        operation_id: str,
        request: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
    ) -> ProfilerAgentResult:
        """Start or resume one profiler conversation on the exact snapshot."""
        conversation = await self._conversation(
            session_id,
            scope_id=scope_id,
            candidate_snapshot_id=candidate_snapshot_id,
        )
        task = asyncio.current_task()
        if task is None:
            message = "profiler turn requires an asyncio task"
            raise RuntimeError(message)
        typed_task = task
        self._operations[operation_id] = typed_task
        prompt = render_template(
            "shared/profiler_turn_prompt.j2",
            candidate_snapshot_id=candidate_snapshot_id,
            request=request,
            evaluation_suspension=(
                AgentCapability.DURABLE_TURN_CONTINUATION in self._role.required_capabilities
            ),
        )
        try:
            if AgentCapability.DURABLE_TURN_CONTINUATION not in self._role.required_capabilities:
                return await structured_turn(conversation.session, prompt, ProfilerAgentResult)

            async def validate(reply: _ProfilerReply) -> None:
                if isinstance(reply.root, _WaitingForEvaluation):
                    access = self._evaluation
                    workspace_id = conversation.workspace.id
                    if access is None or workspace_id is None:
                        message = "profiler wait requires owned evaluation authority"
                        raise RuntimeContractError(message)
                    await access.validate_wait(
                        reply.root.handles,
                        scope_id=workspace_id,
                        principal_id=evaluation_principal(
                            EvaluationAgentRole.PROFILER,
                            conversation.session.member_id,
                            workspace_id,
                        ),
                    )

            reply = (
                await structured_turn(
                    conversation.session,
                    prompt,
                    _ProfilerReply,
                    invocation_id=operation_id,
                    validate_response=validate,
                )
            ).root
            continuation = 0
            while isinstance(reply, _WaitingForEvaluation):
                reports = await self._settle(conversation, reply)
                continuation += 1
                resumed = await conversation.session.resume(
                    render_template("shared/profiler_resume_prompt.j2", results=reports),
                    f"{operation_id}/evaluation/{continuation}",
                    response=_ProfilerReply,
                )
                if not isinstance(resumed, Completed):
                    message = "profiler continuation acceptance requires reconciliation"
                    raise RuntimeContractError(message)
                parsed = _ProfilerReply.model_validate_json(resumed.result.text)
                try:
                    await validate(parsed)
                except EvaluationAgentAccessError as error:
                    parsed = await structured_turn(
                        conversation.session,
                        render_template(
                            "shared/structured_correction_prompt.j2",
                            error=str(error),
                            schema=_ProfilerReply.__name__,
                        ),
                        _ProfilerReply,
                        invocation_id=f"{operation_id}/evaluation/{continuation}/wait-correction",
                        validate_response=validate,
                    )
                reply = parsed.root
        except asyncio.CancelledError:
            await self._drop(session_id)
            raise
        finally:
            self._operations.pop(operation_id, None)
        return reply

    async def _settle(
        self, conversation: _Conversation, reply: _WaitingForEvaluation
    ) -> tuple[dict[str, object], ...]:
        access = self._evaluation
        if access is None or conversation.workspace.id is None:
            message = "profiler suspension requires an owned evaluation workspace"
            raise RuntimeContractError(message)
        evaluation = access.backend
        settlements = access.settlements
        dependencies = OwnedEvaluationDependencies(
            scope_id=conversation.workspace.id,
            generation=await access.requester_generation(
                reply.handles[0], conversation.workspace.id
            ),
            handles=reply.handles,
        )
        observations = await settlements.observe(dependencies)
        pending = reply.handles
        while pending:
            for observation in observations:
                if isinstance(observation.result, EvaluationUnknown):
                    raise RuntimeContractError(observation.result.detail)
            settled = {
                observation.handle_id
                for observation in observations
                if not isinstance(observation.result, EvaluationPending)
            }
            pending = tuple(handle for handle in pending if handle not in settled)
            if pending:
                observations = await settlements.wait_any(
                    dependencies.model_copy(update={"handles": pending})
                )
        return tuple(
            [
                (await evaluation.operation_snapshot(handle)).model_dump(mode="json")
                for handle in reply.handles
            ]
        )

    async def cancel(self, operation_id: str) -> None:
        """Cancel an active turn; its unwind retires the affected conversation."""
        task = self._operations.get(operation_id)
        if task is not None:
            task.cancel()

    async def cancel_scope(self, scope_id: str) -> None:
        """Retire every conversation derived from a discarded candidate scope."""
        session_ids = tuple(
            session_id
            for session_id, conversation in self._conversations.items()
            if conversation.scope_id == scope_id
        )
        for session_id in session_ids:
            await self._drop(session_id)

    async def close(self) -> None:
        """Cancel turns and release every profiler session and workspace."""
        tasks = tuple(self._operations.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session_id in tuple(self._conversations):
            await self._drop(session_id)

    async def _conversation(
        self,
        session_id: str,
        *,
        scope_id: str | None,
        candidate_snapshot_id: str,
    ) -> _Conversation:
        async with self._lock:
            existing = self._conversations.get(session_id)
            if existing is not None:
                await existing.workspace.restore(candidate_snapshot_id)
                return existing
            workspace = await self._workspaces.create_candidate(candidate_snapshot_id)
            try:
                session = await self._agents.create_session(
                    self._role,
                    workspace=workspace,
                    member_id=session_id,
                )
            except BaseException:
                await workspace.discard()
                raise
            conversation = _Conversation(scope_id, workspace, session)
            self._conversations[session_id] = conversation
            return conversation

    async def _drop(self, session_id: str) -> None:
        async with self._lock:
            conversation = self._conversations.pop(session_id, None)
        if conversation is None:
            return

        async def withdraw() -> None:
            access = self._evaluation
            if access is not None:
                if conversation.workspace.id is None:
                    message = "profiler cancellation requires an owned evaluation workspace"
                    raise RuntimeContractError(message)
                await access.cancel_associations(conversation.workspace.id)

        errors: list[BaseException] = []
        # Stop the producer before draining submissions and withdrawing its associations.
        for release in (conversation.session.close, withdraw, conversation.workspace.discard):
            try:
                await release()
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-930062 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
                errors.append(error)
        if errors:
            raise RunCleanupError(_CLEANUP_FAILURE, tuple(errors))


__all__ = ["ProfilerEvaluationAccess", "RuntimeProfilerTurnProvision"]
