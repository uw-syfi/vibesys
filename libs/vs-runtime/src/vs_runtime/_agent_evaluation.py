"""Bridge an agent's in-turn evaluation tool calls to vs-core requests (D396).

An agent submits a measurement of its own workspace from inside a turn. Core stays the
only authority on admission, budget and identity: this module turns each tool call into
one ``AgentMeasurementRequested`` event, asks core what it decides (``CoreRuntime.admit``
runs core's pure step against the committed state and queues the event for the next
commit), and answers the tool with core's decision. It decides nothing itself.

Flow of one turn that waits on its measurements:

1. ``submit_evaluation``: snapshot the caller's workspace, build the plan from the
   host's ``AgentEvaluationPolicy``, admit the event. Core allocates one
   ``SubmitMeasurement`` (or refuses, with a typed reason). The reply is the handle,
   which is the id core and the executor give the measurement's job.
2. ``validate_evaluation_wait``: the handles must be this scope's own admitted
   submissions. The scope's yield is recorded.
3. The agent ends its turn with its waiting reply. When the session executor reports the
   turn, it asks ``yielded`` for the continuation, so the one ``TurnObserved`` carries the
   suspension. The queued event commits before that observation, so the measurement is
   already owned when core validates the continuation, and core resumes the turn once.

There is no status, wait or cancel tool: a suspended caller never polls. Cancel and
deadline are core's: ``DeadlineReached`` resumes with a timeout, and a stopping run drains
its jobs and refuses new calls (``NOT_ADMITTED``).

The yield is held in memory until the turn is reported. A host restart in that window
loses it; the turn then ends without suspending and its measurement result reaches the
strategy as an ordinary ``MeasurementResult``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import TypeAdapter, ValidationError

from vs_core.api import (
    AgentMeasurementRequested,
    AgentRejection,
    ArtifactRef,
    Continuation,
    ContinuationId,
    ContinuationPhase,
    ContractError,
    DispatchTurn,
    InvocationId,
    InvocationRef,
    MeasurementPlan,
    MeasurementStage,
    ResourceId,
    RevisionRef,
    Scope,
    SubmitMeasurement,
    Transition,
)
from vs_evaluation.api import (
    AgentEvaluationCall,
    RunStoppingReply,
    SocketFailure,
    SocketSuccess,
    SubmitCall,
    SubmittedReply,
    WaitCall,
    WaitReply,
)
from vs_evaluation.api.tools import CORE_EVALUATION_TOOLS, core_evaluation_mcp_descriptor
from vs_runtime._evaluation_jobs import handle_for
from vs_runtime._workspace_lookup import find_scope_workspace
from vs_runtime._workspace_requests import revision_ref

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import ToolServerDescriptor
    from vs_core.api import CoreEvent
    from vs_runtime._workspace_receipts import WorkspaceReceipts
    from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces
    from vs_runtime.contracts import AgentRole

EVALUATION_TOOL_ID = "evaluation"
"""The ``AgentRole.extra_tools`` id that asks for this bridge's tools."""

_FRAME_LIMIT = 65_536
_SNAPSHOT_LABEL = "agent-evaluation"
_CALLS = TypeAdapter(AgentEvaluationCall)
_REFUSAL_TEXT = {
    AgentRejection.RUN_STOPPING: "the run is stopping",
    AgentRejection.NOT_ADMITTED: "this workspace is no longer a current owner of evaluations",
    AgentRejection.INVALID_PLAN: "the candidate has no revision to evaluate",
    AgentRejection.NOT_ALLOWED: (
        "the run's submission budget for this exact candidate is spent, or the plan exceeds a "
        "run limit or its deadline; change the candidate before submitting again"
    ),
}


@dataclass(frozen=True)
class AgentEvaluationPolicy:
    """What the host fixes about an agent's measurement; the agent supplies none of it."""

    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    recipe: ArtifactRef
    stages: tuple[tuple[str, float], ...]
    """Stage ids in dependency order with each stage's execution budget in seconds."""
    queue_allowance: float
    accuracy_stage: str | None = None
    purpose: Literal["baseline", "local-validation", "official", "profile"] = "local-validation"

    def plan(self, candidate: RevisionRef, *, now_at: float) -> MeasurementPlan:
        """The ordered plan over the declared stages for this candidate, submitted now."""
        stages = tuple(
            MeasurementStage(
                stage_id=name,
                depends_on=self.stages[index - 1][:1] if index else (),
                execution_budget=seconds,
            )
            for index, (name, seconds) in enumerate(self.stages)
        )
        return MeasurementPlan(
            purpose=self.purpose,
            candidate=candidate,
            evaluator_digest=self.evaluator_digest,
            workload_digest=self.workload_digest,
            environment_digest=self.environment_digest,
            stages=stages,
            policy="ordered",
            recipe=self.recipe,
            submitted_at=now_at,
            queue_allowance=self.queue_allowance,
            deadline_at=now_at + sum(s.execution_budget for s in stages) + self.queue_allowance,
            accuracy_stage=self.accuracy_stage,
        )


class AgentWorkspaces(Protocol):
    """Finds the live workspace a scope's agent is working in."""

    async def workspace_of(self, scope: Scope) -> RuntimeWorkspace | None:
        """The scope's live workspace, or None when it no longer exists."""
        ...


class ScopeWorkspaces:
    """The run's own lookup: a scope's workspace, from the workspaces and their receipts."""

    def __init__(self, workspaces: RuntimeWorkspaces, receipts: WorkspaceReceipts) -> None:
        """Bind the host's live workspaces and the receipts that map attempts to them."""
        self._workspaces = workspaces
        self._receipts = receipts

    async def workspace_of(self, scope: Scope) -> RuntimeWorkspace | None:
        """The scope's live workspace, reopened from disk after a restart; None if gone."""
        return await find_scope_workspace(self._workspaces, self._receipts, scope)


class AdmissionShell(Protocol):
    """The shell as the bridge sees it: ask core, and queue the event for commit."""

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """Run core's step on the event and queue it; ``ContractError`` queues nothing."""
        ...


class Clock(Protocol):
    """The run's time source."""

    def now(self) -> float:
        """Seconds on the run's timeline."""
        ...


@dataclass
class _Scoped:
    """One scope's in-flight submissions and its recorded yield."""

    submitted: dict[str, float]
    """Handle to the measurement's deadline, for each admitted submission not yet yielded."""
    last_revision: str | None = None
    waits: tuple[str, ...] = ()


class AgentEvaluationBridge:
    """The unix-socket service, token minting and yield producer of one run.

    ``serve`` before agents run, ``close`` after. ``attach`` binds the run's shell and
    clock once the shell exists. Pass ``servers`` as the resolver's tool source and
    ``yielded`` as the session executor's ``TurnYields``.
    """

    def __init__(
        self,
        socket_path: Path,
        policy: AgentEvaluationPolicy,
        workspaces: AgentWorkspaces,
    ) -> None:
        """Bind the socket path, the host's plan policy and the workspace lookup."""
        self._socket_path = socket_path
        self._policy = policy
        self._workspaces = workspaces
        self._secret = os.urandom(32)
        self._shell: AdmissionShell | None = None
        self._clock: Clock | None = None
        self._scopes: dict[str, _Scoped] = {}
        self._server: asyncio.AbstractServer | None = None
        self._lock = asyncio.Lock()

    def attach(self, shell: AdmissionShell, clock: Clock) -> None:
        """Bind the run's shell and clock, once the shell exists."""
        self._shell = shell
        self._clock = clock

    # tool offer

    def servers(self, role: AgentRole, scope: Scope) -> tuple[ToolServerDescriptor, ...]:
        """The evaluation tool server for roles that declare it; none for any other role."""
        if not any(tool.id == EVALUATION_TOOL_ID for tool in role.extra_tools):
            return ()
        return (core_evaluation_mcp_descriptor(self._token(scope), str(self._socket_path)),)

    def _token(self, scope: Scope) -> str:
        body = base64.urlsafe_b64encode(scope.model_dump_json().encode()).decode()
        mac = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def _scope_of(self, token: str) -> Scope:
        body, _, mac = token.partition(".")
        expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            message = "unknown evaluation token"
            raise PermissionError(message)
        return Scope.model_validate_json(base64.urlsafe_b64decode(body))

    # socket service

    async def serve(self) -> None:
        """Listen on the socket path; the owner of the path is this bridge."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._client, path=str(self._socket_path), limit=_FRAME_LIMIT
        )

    async def close(self) -> None:
        """Stop accepting calls and remove the socket."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            frame = await reader.readline()
            reply = await self.handle(frame)
            writer.write(reply + b"\n")
            await writer.drain()
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    async def handle(self, frame: bytes) -> bytes:
        """Answer one wire frame: a typed success, or a typed failure naming the problem."""
        try:
            call = _CALLS.validate_json(frame)
            if isinstance(call, SubmitCall):
                result = await self._submit(self._scope_of(call.token))
            elif isinstance(call, WaitCall):
                result = self._validate_wait(self._scope_of(call.token), call.handles)
            else:
                message = f"{call.action} is not offered on a core run"
                return self._failure(message)
        except ValidationError:
            return self._failure("malformed evaluation call")
        except (PermissionError, ValueError, ContractError) as error:
            return self._failure(str(error))
        return SocketSuccess(result=result.model_dump(mode="json")).model_dump_json().encode()

    @staticmethod
    def _failure(message: str) -> bytes:
        return SocketFailure(error=message).model_dump_json().encode()

    # submit

    async def _submit(self, scope: Scope) -> SubmittedReply | RunStoppingReply:
        shell, clock = self._bound()
        async with self._lock:
            workspace = await self._workspaces.workspace_of(scope)
            if workspace is None:
                message = "this workspace no longer exists"
                raise ValueError(message)
            scoped = self._scopes.setdefault(scope.model_dump_json(), _Scoped(submitted={}))
            revision = await self._revision(workspace, scoped)
            now = clock.now()
            call_id = f"agent:{uuid.uuid4().hex}"
            event = AgentMeasurementRequested(
                scope=scope,
                plan=self._policy.plan(revision_ref(revision), now_at=now),
                call_id=call_id,
            )
            transition = shell.admit(event, now_at=now)
            request = next(
                (r for r in transition.requests if isinstance(r, SubmitMeasurement)), None
            )
            if request is None or request.request_id is None:
                return self._refusal(transition, call_id)
            handle = handle_for(request.request_id)
            scoped.submitted[handle] = request.deadline_at
            return SubmittedReply(handle_id=handle)

    @staticmethod
    async def _revision(workspace: RuntimeWorkspace, scoped: _Scoped) -> str:
        """The workspace's content as a revision, reusing the last when nothing changed.

        Identity includes the revision, so a snapshot that is not content-addressed
        would give every call a fresh identity and its own budget.
        """
        last = scoped.last_revision
        if last is not None and await workspace.matches_revision(last):
            return last
        revision = await workspace.snapshot_and_retain(
            _SNAPSHOT_LABEL, retention_label=_SNAPSHOT_LABEL
        )
        scoped.last_revision = revision
        return revision

    @staticmethod
    def _refusal(transition: Transition, call_id: str) -> RunStoppingReply:
        call = next(
            (c for c in transition.state.evaluation.agent_calls if c.call_id == call_id), None
        )
        if call is None or call.rejection is None:
            message = "core neither admitted nor refused the evaluation call"
            raise ContractError(("agent_calls",), message)
        if call.rejection is AgentRejection.RUN_STOPPING:
            return RunStoppingReply()
        raise ValueError(_REFUSAL_TEXT[call.rejection])

    # wait and yield

    def _validate_wait(self, scope: Scope, handles: tuple[str, ...]) -> WaitReply:
        scoped = self._scopes.get(scope.model_dump_json())
        owned = scoped.submitted if scoped is not None else {}
        unknown = sorted(set(handles) - owned.keys())
        if scoped is None or unknown:
            message = (
                f"handles {unknown} were not submitted by this agent in this turn; "
                "only your own submissions can be waited on"
            )
            raise ValueError(message)
        scoped.waits = tuple(dict.fromkeys(handles))
        return WaitReply(handles=scoped.waits)

    def yielded(self, request: DispatchTurn) -> Continuation | None:
        """The continuation of a turn whose agent validated a wait, once; else None."""
        scoped = self._scopes.get(request.scope.model_dump_json())
        if scoped is None or not scoped.waits:
            return None
        handles, scoped.waits = scoped.waits, ()
        deadline = min(scoped.submitted.pop(handle) for handle in handles)
        turn = request.turn
        session, generation = turn.session.session_id, request.scope.generation
        return Continuation(
            continuation_id=ContinuationId(root=f"{turn.invocation_id.root}/evaluation"),
            invocation=InvocationRef(
                session_id=session, invocation_id=turn.invocation_id, generation=generation
            ),
            next_invocation=InvocationRef(
                session_id=session,
                invocation_id=InvocationId(root=f"{turn.invocation_id.root}/resume"),
                generation=generation,
            ),
            jobs=tuple(ResourceId(root=handle) for handle in handles),
            deadline_at=deadline,
            phase=ContinuationPhase.WAITING,
        )

    def _bound(self) -> tuple[AdmissionShell, Clock]:
        if self._shell is None or self._clock is None:
            message = "the evaluation bridge is not attached to a run"
            raise RuntimeError(message)
        return self._shell, self._clock


__all__ = [
    "CORE_EVALUATION_TOOLS",
    "EVALUATION_TOOL_ID",
    "AgentEvaluationBridge",
    "AgentEvaluationPolicy",
    "AgentWorkspaces",
    "ScopeWorkspaces",
]
