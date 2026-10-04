"""One-shot crash boundaries around faithful in-memory effect implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_runtime.api.infrastructure import prepare_agent_conversation

if TYPE_CHECKING:
    from pathlib import PurePosixPath

    from pydantic import BaseModel

    from vs_agent.api import AgentSessionKey, InvocationOutcome
    from vs_evaluation.api import EvaluationStateNamespace
    from vs_project.api import StateModels
    from vs_runtime.api import (
        AgentConversationRequest,
        AgentRole,
        AgentSession,
        PreparedConversation,
        State,
        Workspace,
        WorkspaceAgentSessions,
    )


class Boundary(StrEnum):
    PREPARE = "prepare"
    DISPATCH = "dispatch"
    SETTLE = "settle"
    RELEASE = "release"
    SUBMIT = "submit"
    CANCEL = "cancel"
    SESSION = "session"


class Side(StrEnum):
    BEFORE = "before"
    AFTER = "after"


class TraceCrash(BaseException):
    """A process crash cannot be converted into an ordinary retry."""


@dataclass
class FaultBoundary:
    boundary: Boundary
    side: Side
    armed: bool = False
    fired: bool = False
    reached: asyncio.Event = field(default_factory=asyncio.Event)

    def hit(self, boundary: Boundary, side: Side) -> None:
        if self.armed and not self.fired and (self.boundary, self.side) == (boundary, side):
            self.fired = True
            self.reached.set()
            raise TraceCrash(f"{boundary.value}:{side.value}")


class FaultState:
    """The actual State port with failures before and after atomic replacement."""

    def __init__(self, delegate: State, fault: FaultBoundary) -> None:
        self.delegate = delegate
        self.fault = fault

    def namespace(self, name: str) -> StateModels:
        """Preserve the host namespace seam while faulting plugin checkpoints."""
        return self.delegate.namespace(name)

    async def load[ModelT: BaseModel](self, model: type[ModelT]) -> ModelT | None:
        return await self.delegate.load(model)

    async def commit(
        self, value: BaseModel, *, workspace: Workspace | None = None, label: str | None = None
    ) -> None:
        boundary = None
        if label == "dynamic: a implementing":
            boundary = Boundary.PREPARE
        elif label == "dynamic: a dynamic-implementer dispatch authorized":
            boundary = Boundary.DISPATCH
        elif label is not None and ("record hypothesis" in label or label.endswith("cancelled")):
            boundary = Boundary.SETTLE
        if boundary is not None:
            self.fault.hit(boundary, Side.BEFORE)
        await self.delegate.commit(value, workspace=workspace, label=label)
        if boundary is not None:
            self.fault.hit(boundary, Side.AFTER)


class FaultSessions:
    """Session ownership is real even when its creation response is lost."""

    def __init__(self, delegate: WorkspaceAgentSessions, fault: FaultBoundary) -> None:
        self.delegate = delegate
        self.fault = fault

    def prepare_conversation(self, request: AgentConversationRequest) -> PreparedConversation:
        return prepare_agent_conversation(self, request)

    def inspect_invocation(self, key: AgentSessionKey, invocation_id: str) -> InvocationOutcome:
        return self.delegate.inspect_invocation(key, invocation_id)

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        generation: int | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        if member_id == "a":
            self.fault.hit(Boundary.SESSION, Side.BEFORE)
        result = await self.delegate.create_session(
            role,
            workspace=workspace,
            member_id=member_id,
            generation=generation,
            writable_paths=writable_paths,
        )
        if member_id == "a":
            self.fault.hit(Boundary.SESSION, Side.AFTER)
        return result

    async def close(self) -> None:
        await self.delegate.close()


class FaultNamespace:
    """Delegate storage with a crash on either side of the release-intent write."""

    def __init__(self, delegate: EvaluationStateNamespace, fault: FaultBoundary) -> None:
        self.delegate = delegate
        self.fault = fault

    def load[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT:
        return self.delegate.load(relative_path, model_type)

    def load_optional[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT | None:
        return self.delegate.load_optional(relative_path, model_type)

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        release = str(relative_path) == "agent-evaluation-released-scopes.json"
        if release:
            self.fault.hit(Boundary.RELEASE, Side.BEFORE)
        self.delegate.save(relative_path, model)
        if release:
            self.fault.hit(Boundary.RELEASE, Side.AFTER)

    def delete(self, relative_path: str | PurePosixPath) -> bool:
        return self.delegate.delete(relative_path)
