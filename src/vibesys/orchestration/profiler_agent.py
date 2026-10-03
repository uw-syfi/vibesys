"""Runtime-backed provision for delegated profiler-agent conversations."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.prompts import render_template
from vibesys.orchestration.structured_turn import structured_turn
from vs_evaluation.api import ProfilerAgentResult

if TYPE_CHECKING:
    from vs_runtime.api import (
        AgentRole,
        AgentSession,
        AgentSessions,
        CandidateWorkspace,
        Workspaces,
    )


_CLEANUP_FAILURE = "profiler conversation cleanup failed"


@dataclass(slots=True)
class _Conversation:
    scope_id: str | None
    workspace: CandidateWorkspace
    session: AgentSession


class RuntimeProfilerTurnProvision:
    """Create resumable profiler sessions through public runtime capabilities."""

    def __init__(
        self,
        role: AgentRole,
        agents: AgentSessions,
        workspaces: Workspaces,
    ) -> None:
        """Bind the profiler role to run-owned agent and workspace capabilities."""
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
        )
        try:
            return await structured_turn(conversation.session, prompt, ProfilerAgentResult)
        except asyncio.CancelledError:
            await self._drop(session_id)
            raise
        finally:
            self._operations.pop(operation_id, None)

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
        errors: list[BaseException] = []
        try:
            await conversation.session.close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-930062 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
            errors.append(error)
        try:
            await conversation.workspace.discard()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-930063 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
            errors.append(error)
        if errors:
            raise BaseExceptionGroup(_CLEANUP_FAILURE, errors)


__all__ = ["RuntimeProfilerTurnProvision"]
