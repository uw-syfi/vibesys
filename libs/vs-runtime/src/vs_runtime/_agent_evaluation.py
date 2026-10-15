"""Bridge an agent's in-turn evaluation tool calls to vs-core requests (D396).

An agent submits a measurement of its own workspace from inside a turn. Core stays the
only authority on admission, budget and identity: this module turns each tool call into
one ``AgentMeasurementRequested`` event, asks core what it decides (``CoreRuntime.admit``
runs core's pure step against the committed state and queues the event for the next
commit), and answers the tool with core's decision. It decides nothing itself.

Flow of one turn that waits on its measurements:

1. ``submit_evaluation``: snapshot the caller's workspace, build the plan from the
   host's ``AgentEvaluationPolicy``, admit the event. Core allocates one
   ``SubmitMeasurement``, names the one the scope already holds for this candidate, or
   refuses with a typed reason. The reply is the handle, which is the id core and the
   executor give the measurement's job.
2. ``validate_evaluation_wait``: the handles must be this scope's own admitted
   submissions, and core must accept a suspension for the scope now
   (``suspension_refusal``: no earlier resume owed, scope active). Otherwise the agent
   gets a typed, template-rendered tool error and nothing is recorded. The scope's yield
   is recorded.
3. The agent ends its turn with its waiting reply. When the session executor reports the
   turn, it asks ``yielded`` for the continuation, so the one ``TurnObserved`` carries the
   suspension. The queued event commits before that observation, so the measurement is
   already owned when core validates the continuation, and core resumes the turn once.

There is no status, wait or cancel tool: a suspended caller never polls. Cancel and
deadline are core's: ``DeadlineReached`` resumes with a timeout, and a stopping run drains
its jobs and refuses new calls (``NOT_ADMITTED``).

Every failure of a call is a ``ToolRefusal`` answered to the agent; none reaches the
shell's commit. ``yielded`` asks core again when the turn is reported and, if the scope
changed since the wait was validated, ends the turn without suspending.

One evaluation per candidate and scope. A call for a candidate its scope already evaluates
(same content and measurement policy, so the same ``MeasurementIdentity``) is answered with
that evaluation's handle, with its ``outcome`` and evidence ids once it has ended, and
charges nothing. Core decides this (``AgentCall.repeat``): the scope's submission budget is
the one record of "this candidate's evaluation", and this bridge keeps none of its own. The
scope is a workstream's attempt, so the roles that hold it (an implementer, then the judge
reviewing its candidate) read the same evaluation; another attempt, even with identical
content, has its own, because its evidence and budget are its own. Only a released,
retryable infrastructure failure lets the next call start a new submission.

A submission the agent has not learned of is never lost. The reply that carries a handle
may not reach the agent (its tool deadline expired while the host snapshotted the workspace
behind a busy lock, or the agent's process died). A submission stays *owed* until the client
confirms it read the reply: the next ``submit_evaluation`` for the same candidate content,
in this turn or a later one, returns that handle, repeat or not. ``unacknowledged`` lists
what is still owed, for the run loop's stall report.

What is owed is durable (``OwedSubmissions``, keyed by scope, candidate revision and
policy), so a host restart keeps the promise. The entry is written before the call, with
the ``call_id`` the call will carry; core's committed state says whether the call happened.
A retry finds the entry, asks core for that ``call_id``, and either returns the answer core
holds or, when the call never committed, makes it under a new entry. Core admits a
``call_id`` once, so one entry is never answered twice.

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
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import TypeAdapter

from vs_core.api import (
    AgentCall,
    AgentMeasurementRequested,
    AgentRejection,
    ArtifactRef,
    Continuation,
    ContinuationId,
    ContinuationPhase,
    ContractError,
    DispatchTurn,
    EvidenceId,
    EvidenceKey,
    EvidenceKind,
    InvocationId,
    InvocationRef,
    MeasurementPlan,
    MeasurementRecipe,
    PreparedSubmissionReceipt,
    ResourceId,
    RevisionRef,
    Scope,
    SessionPhase,
    StageBudget,
    SuspensionRefusal,
    Transition,
    Within,
    suspension_refusal,
)
from vs_decode.api import Unreadable, decode_json
from vs_evaluation.api import (
    RECEIPT_LINE,
    AgentEvaluationCall,
    EvaluationStageOutcome,
    RunStoppingReply,
    SocketFailure,
    SocketSuccess,
    SubmitCall,
    SubmittedReply,
    WaitCall,
    WaitReply,
)
from vs_evaluation.api.tools import (
    CORE_EVALUATION_TOOLS,
    core_evaluation_mcp_descriptor,
)
from vs_project.api import NestedRepositoryError
from vs_runtime._agent_tool_errors import ToolRefusal, ToolRefusedError, render_tool_refusal
from vs_runtime._core_loop import AdmissionBusyError
from vs_runtime._evaluation_jobs import handle_for
from vs_runtime._owed_submissions import OwedKey, OwedSubmission, OwedSubmissions
from vs_runtime._workspace_lookup import find_scope_workspace
from vs_runtime._workspace_requests import revision_ref

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from vs_core.api import CoreEvent, CoreState, RequestId
    from vs_evaluation.api import TrustedEvidence
    from vs_mcp.api import ToolServerDescriptor
    from vs_runtime._evidence_ledger import EvidenceLookup
    from vs_runtime._workspace_receipts import WorkspaceReceipts
    from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces
    from vs_runtime.contracts import AgentRole
    from vs_sim.api import Clock, Connection, Listener, Network, Threads

EVALUATION_TOOL_ID = "evaluation"
"""The ``AgentRole.extra_tools`` id that asks for this bridge's tools."""

_FRAME_LIMIT = 65_536
_SNAPSHOT_LABEL = "agent-evaluation"
_CALLS = TypeAdapter(AgentEvaluationCall)
_REFUSALS = {
    AgentRejection.RUN_STOPPING: ToolRefusal.RUN_STOPPING,
    AgentRejection.NOT_ADMITTED: ToolRefusal.NOT_ADMITTED,
    AgentRejection.INVALID_PLAN: ToolRefusal.INVALID_PLAN,
    AgentRejection.NOT_ALLOWED: ToolRefusal.NOT_ALLOWED,
}
_WAIT_REFUSALS = {
    SuspensionRefusal.OPEN_CONTINUATION: ToolRefusal.WAIT_OPEN,
    SuspensionRefusal.SCOPE_NOT_ACTIVE: ToolRefusal.WAIT_NOT_ACTIVE,
    SuspensionRefusal.NOT_RESUMABLE: ToolRefusal.WAIT_NOT_RESUMABLE,
    SuspensionRefusal.PREFIX_MISMATCH: ToolRefusal.WAIT_OVERLAPPED,
    SuspensionRefusal.JOB_NOT_OWNED: ToolRefusal.UNKNOWN_HANDLES,
    SuspensionRefusal.DEADLINE_EXCEEDED: ToolRefusal.WAIT_DEADLINE,
}


def _owned_wait(
    core: CoreState, current: InvocationRef, handles: tuple[str, ...], deadlines: dict[str, float]
) -> Continuation | None:
    """The wait to check against core's job rules, or None while a submission is uncommitted.

    A submission owns its job once its request's observation commits, and the loop commits
    that after the agent's call returns. Until then core cannot say the job is owned, so only
    the scope rules apply; the commit of the turn's suspension follows the submission's.
    """
    owned = {
        row.resource_id.root
        for row in (*core.evaluation.jobs, *core.evaluation.registered_jobs)
        if row.resource_id is not None
    }
    if not set(handles) <= owned:
        return None
    return _continuation(current, handles, deadlines)


def _continuation(
    current: InvocationRef, handles: tuple[str, ...], deadlines: dict[str, float]
) -> Continuation:
    """The wait on ``handles`` that ends ``current``'s turn and resumes in its successor."""
    root = current.invocation_id.root
    return Continuation(
        continuation_id=ContinuationId(root=f"{root}/evaluation"),
        invocation=current,
        next_invocation=current.model_copy(
            update={"invocation_id": InvocationId(root=f"{root}/resume")}
        ),
        jobs=tuple(ResourceId(root=handle) for handle in handles),
        deadline_at=min(deadlines[handle] for handle in handles),
        phase=ContinuationPhase.WAITING,
    )


def _outcome(core: CoreState, request_id: RequestId) -> str | None:
    """How the submission ended per core's budget receipt, or None while it has not."""
    for budget in core.evaluation.submission_budgets:
        for receipt in budget.receipts:
            if isinstance(receipt, PreparedSubmissionReceipt) and receipt.request_id == request_id:
                seen = receipt.observation
                return seen.status.value if seen is not None and seen.terminal else None
    return None


def _read_frame(connection: Connection) -> bytes | None:
    """One line with its newline, or what came before the peer closed.

    ``None`` when the line outgrows ``_FRAME_LIMIT``: the call is dropped unanswered.
    """
    data = bytearray()
    while (end := data.find(b"\n")) < 0:
        if len(data) > _FRAME_LIMIT:
            return None
        chunk = connection.recv(_FRAME_LIMIT)
        if not chunk:
            return bytes(data)
        data += chunk
    return None if end > _FRAME_LIMIT else bytes(data[: end + 1])


def _ignore_diagnostic(line: str) -> None:
    del line


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
    accuracy_stage: str | None
    accuracy_gated: tuple[EvidenceKind, ...]
    """Evidence kinds that count only if the accuracy stage passed: the task's trust rule."""
    purpose: Literal["baseline", "local-validation", "official", "profile"] = "local-validation"

    @classmethod
    def from_recipe(
        cls,
        recipe: MeasurementRecipe,
        *,
        purpose: Literal[
            "baseline", "local-validation", "official", "profile"
        ] = "local-validation",
    ) -> AgentEvaluationPolicy:
        """The agent's policy over exactly the stages ``recipe`` holds."""
        return cls(
            evaluator_digest=recipe.evaluator_digest,
            workload_digest=recipe.workload_digest,
            environment_digest=recipe.environment_digest,
            recipe=recipe.recipe,
            stages=tuple((item.stage_id, item.execution_seconds) for item in recipe.stages),
            queue_allowance=recipe.queue_allowance,
            accuracy_stage=recipe.accuracy_stage,
            accuracy_gated=recipe.accuracy_gated,
            purpose=purpose,
        )

    @property
    def measurement(self) -> MeasurementRecipe:
        """The measurement recipe this policy fixes; the one place its plan is assembled."""
        return MeasurementRecipe(
            evaluator_digest=self.evaluator_digest,
            workload_digest=self.workload_digest,
            environment_digest=self.environment_digest,
            recipe=self.recipe,
            stages=tuple(
                StageBudget(stage_id=name, execution_seconds=sec) for name, sec in self.stages
            ),
            queue_allowance=self.queue_allowance,
            accuracy_stage=self.accuracy_stage,
            accuracy_gated=self.accuracy_gated,
        )

    def plan(self, candidate: RevisionRef, *, now_at: float) -> MeasurementPlan:
        """The ordered plan over the declared stages for this candidate, submitted now."""
        return self.measurement.plan(candidate, purpose=self.purpose, now_at=now_at)


class SnapshotWorkspace(Protocol):
    """What the bridge needs of an agent's workspace: its content as a revision."""

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        """Commit the workspace's content and keep it reachable; the commit id."""
        ...

    async def matches_revision(self, revision: str) -> bool:
        """Whether the workspace's content is exactly ``revision``."""
        ...


class AgentWorkspaces(Protocol):
    """Finds the live workspace a scope's agent is working in."""

    async def workspace_of(self, scope: Scope) -> SnapshotWorkspace | None:
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


class _Envelope(Protocol):
    @property
    def core(self) -> CoreState: ...


class _Record(Protocol):
    @property
    def envelope(self) -> _Envelope: ...


class AdmissionShell(Protocol):
    """The shell as the bridge sees it: ask core, and queue the event for commit."""

    @property
    def record(self) -> _Record:
        """The committed record; the loop may commit while an agent turn runs."""
        ...

    @property
    def admitted_core(self) -> CoreState:
        """Core's state including the admitted inputs not yet committed."""
        ...

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """Run core's step on the event and queue it; ``ContractError`` queues nothing."""
        ...


def _nothing() -> None:
    """The acknowledgement of an answer that carries no handle."""


@dataclass
class _Scoped:
    """One scope's last snapshot, and the submissions and recorded wait of its current turn."""

    last_revision: str | None = None
    turn: InvocationId | None = None
    """The turn that made ``submitted`` and ``waits``; another turn's call discards them."""
    submitted: dict[str, float] = field(default_factory=dict)
    """Handle to the measurement's deadline, for each submission of ``turn`` not yet yielded."""
    waits: tuple[str, ...] = ()

    def begin(self, turn: InvocationId | None) -> None:
        """Join ``turn``; a handle or wait of an earlier turn is not this turn's to use."""
        if turn != self.turn:
            self.turn = turn
            self.submitted = {}
            self.waits = ()


@dataclass(frozen=True, slots=True)
class AgentEvaluationRecords:
    """The durable records the bridge reads and writes.

    ``owed`` keeps a charged submission until its reply reached the agent. ``evidence`` is the
    ledger the evaluation executor records every accepted result in; a reply that reports a
    finished evaluation reads what it measured from there.
    """

    owed: OwedSubmissions
    evidence: EvidenceLookup


class AgentEvaluationBridge:
    """The unix-socket service, token minting and yield producer of one run.

    ``serve`` before agents run, ``close`` after. ``attach`` binds the run's shell and
    clock once the shell exists. Pass ``servers`` as the resolver's tool source and
    ``yielded`` as the session executor's ``TurnYields``.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-241401 [PLR0913]; the service's independent collaborators plus the network it serves on.
        # > A parameter object for the path, network and threads would be a type with one
        # > user that only renames three arguments; reading them off a module global would
        # > hide from tests the seams that let them run the service in memory and seeded.
        self,
        socket_path: Path,
        policy: AgentEvaluationPolicy,
        workspaces: AgentWorkspaces,
        records: AgentEvaluationRecords,
        diagnostics: Callable[[str], None] = _ignore_diagnostic,
        *,
        network: Network,
        threads: Threads,
    ) -> None:
        """Bind the socket path, the plan policy, the workspace lookup, the records and a log sink.

        ``diagnostics`` hears one line for each call refused as busy, for the run's journal.
        ``network`` binds the socket path and ``threads`` reads each connection.
        """
        self._socket_path = socket_path
        self._policy = policy
        self._workspaces = workspaces
        self._owed = records.owed
        self._evidence = records.evidence
        self._policy_digest = hashlib.sha256(
            f"{policy.purpose}\n{policy.measurement.model_dump_json()}".encode()
        ).hexdigest()
        self._diagnostics = diagnostics
        self._secret = os.urandom(32)
        self._shell: AdmissionShell | None = None
        self._clock: Clock | None = None
        self._scopes: dict[str, _Scoped] = {}
        self._network = network
        self._threads = threads
        self._listener: Listener | None = None
        self._guard = self._threads.lock()
        self._open: set[Connection] = set()
        self._closing = False
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
        scope = decode_json(
            base64.urlsafe_b64decode(body), Scope, source="evaluation token", max_bytes=_FRAME_LIMIT
        )
        if isinstance(scope, Unreadable):
            raise PermissionError(str(scope))
        return scope

    # socket service

    async def serve(self) -> None:
        """Listen on the socket path, in a directory the caller created private.

        The bridge never creates that directory: a create that accepts an existing
        path would serve inside one another user planted, who could then replace
        the socket and answer the agent's evaluation calls. Each connection is read
        on its own worker thread; its call is answered on this event loop.
        """
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        loop = asyncio.get_running_loop()
        listener = self._network.listen(str(self._socket_path))
        self._listener = listener
        self._threads.spawn(
            partial(self._accept_loop, listener, loop), name="agent-evaluation-bridge"
        )

    async def close(self) -> None:
        """Stop accepting calls, hang up on every open connection, and remove the socket.

        A client that never finishes its call cannot hold the run open: hanging up wakes
        the worker reading from it.
        """
        listener, self._listener = self._listener, None
        with self._guard:
            self._closing = True
            open_connections, self._open = self._open, set()
        if listener is not None:
            listener.close()
        for connection in open_connections:
            connection.close()
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    def _accept_loop(self, listener: Listener, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            try:
                connection = listener.accept()
            except OSError:
                return  # the listener was closed
            with self._guard:
                closing = self._closing
                if not closing:
                    self._open.add(connection)
            if closing:
                connection.close()
                return
            self._threads.spawn(
                partial(self._client, connection, loop), name="agent-evaluation-call"
            )

    def _client(self, connection: Connection, loop: asyncio.AbstractEventLoop) -> None:
        try:
            frame = _read_frame(connection)
            if frame is None:
                return
            answered = self._on_loop(loop, self.answer(frame))
            if answered.cancelled():
                return  # the loop is shutting down
            try:
                reply, acknowledge = answered.result()
            except Exception:
                # Run state the bridge cannot trust: the shell's halt ends the run. Tell
                # the agent to stop instead of retrying, then let the error surface.
                connection.send(self._failure(ToolRefusal.RUN_FAILING) + b"\n")
                raise
            connection.send(reply + b"\n")
            # The client says it read the whole reply before it closes; an abandoned
            # connection ends without the line and leaves the submission owed.
            if _read_frame(connection) == RECEIPT_LINE:
                loop.call_soon_threadsafe(acknowledge)
        except OSError:
            pass
        finally:
            with self._guard:
                self._open.discard(connection)
            connection.close()

    def _on_loop[T](
        self, loop: asyncio.AbstractEventLoop, work: Coroutine[object, object, T]
    ) -> asyncio.Task[T]:
        """Run ``work`` as a task on ``loop`` and wait, on this worker, until it is done."""
        done = self._threads.event()
        tasks: list[asyncio.Task[T]] = []

        def start() -> None:
            task = loop.create_task(work)
            tasks.append(task)
            task.add_done_callback(lambda _task: done.set())

        loop.call_soon_threadsafe(start)
        done.wait()
        return tasks[0]

    async def answer(self, frame: bytes) -> tuple[bytes, Callable[[], None]]:
        """The reply to one frame, and the call that records that the agent received it.

        A transport calls the second value once the client confirmed it read the whole
        reply. A submission whose reply was never confirmed stays owed to the agent (see
        the module docstring). There is deliberately no entry point that confirms on the
        caller's behalf: acknowledging before the caller holds the bytes would clear the
        owed record while the reply could still be lost.
        """
        acknowledge = _nothing
        try:
            call = decode_json(frame, _CALLS, source="evaluation tool call", max_bytes=_FRAME_LIMIT)
            if isinstance(call, Unreadable):
                return self._failure(ToolRefusal.MALFORMED), acknowledge
            if isinstance(call, SubmitCall):
                result, acknowledge = await self._submit(self._scope_of(call.token))
            elif isinstance(call, WaitCall):
                result = self._validate_wait(self._scope_of(call.token), call.handles)
            else:
                return self._failure(ToolRefusal.REFUSED), acknowledge
        except PermissionError:
            return self._failure(ToolRefusal.UNKNOWN_CALLER), acknowledge
        except ToolRefusedError as error:
            return self._failure(error.reason, error), acknowledge
        reply = SocketSuccess(result=result.model_dump(mode="json")).model_dump_json().encode()
        return reply, acknowledge

    def unacknowledged(self) -> tuple[str, ...]:
        """One line per charged submission whose handle never reached its agent.

        An owed entry is charged only if core holds its ``call_id``; one core does not know
        was never charged (the host died first) and is not listed.
        """
        shell, _ = self._bound()
        lines: list[str] = []
        for item in self._owed.all():
            charged = self._charge_of(shell, item)
            if charged is not None and charged.request_id is not None:
                lines.append(
                    f"{handle_for(charged.request_id)} (candidate {item.key.revision[:12]}, "
                    f"scope {item.key.scope.model_dump_json()})"
                )
        return tuple(lines)

    @staticmethod
    def _failure(reason: ToolRefusal, error: ToolRefusedError | None = None) -> bytes:
        text = str(error) if error is not None else render_tool_refusal(reason)
        return SocketFailure(error=text).model_dump_json().encode()

    # submit

    async def _submit(
        self, scope: Scope
    ) -> tuple[SubmittedReply | RunStoppingReply, Callable[[], None]]:
        """Admit the caller's candidate, or hand back the handle of its evaluation.

        Core decides whether the call is a new submission or a repeat of one the scope
        already holds (see ``AgentCall.repeat``); the reply carries the handle either way.
        The second value acknowledges the handle once its reply reached the agent.
        """
        shell, clock = self._bound()
        if self._executing(scope) is None:
            # A stale token: the turn it was issued for ended. Core takes no submission
            # from a scope that has no turn running.
            raise ToolRefusedError(ToolRefusal.TURN_ENDED)
        async with self._lock:
            workspace = await self._workspaces.workspace_of(scope)
            if workspace is None:
                raise ToolRefusedError(ToolRefusal.WORKSPACE_GONE)
            scoped = self._scoped(scope)
            revision = await self._revision(workspace, scoped)
            key = OwedKey(scope=scope, revision=revision, policy=self._policy_digest)
            owed = self._owed.find(key)
            charged = None if owed is None else self._charge_of(shell, owed)
            if owed is not None and charged is not None:
                # A retry of a call whose reply was lost, possibly before a restart: the
                # same answer, not a second call.
                return self._answer(shell, scoped, charged), self._acknowledger(owed)
            now = clock.now()
            # RequestRecord first: if the host dies after the charge commits, the entry names it.
            owed = OwedSubmission(key=key, call_id=f"agent:{uuid.uuid4().hex}", submitted_at=now)
            self._owed.record(owed)
            try:
                event = AgentMeasurementRequested(
                    scope=scope,
                    plan=self._policy.plan(revision_ref(revision), now_at=now),
                    within=Within(limit=self._policy.measurement.budget()),
                    call_id=owed.call_id,
                )
                transition = self._admit(shell, event, now)
                call = self._call_of(transition.state, owed.call_id)
                if call.request_id is None:
                    reply = self._refusal(call)
                    self._owed.clear(key, owed.call_id)
                    return reply, _nothing
            except BaseException:
                self._owed.clear(key, owed.call_id)
                raise
            return self._answer(shell, scoped, call), self._acknowledger(owed)

    def _answer(self, shell: AdmissionShell, scoped: _Scoped, call: AgentCall) -> SubmittedReply:
        """The reply for a call core answered with a submission, which the turn may now wait on."""
        if call.request_id is None or call.deadline_at is None:
            message = "an answered agent call names its submission and deadline"
            raise ContractError(("agent_calls",), message)
        handle = handle_for(call.request_id)
        scoped.submitted[handle] = call.deadline_at
        outcome = _outcome(shell.admitted_core, call.request_id)
        records = shell.admitted_core.evaluation.evidence if outcome is not None else ()
        own = tuple(item for item in records if item.source_request == call.request_id)
        return SubmittedReply(
            handle_id=handle,
            repeat=call.repeat,
            outcome=outcome,
            evidence=tuple(item.evidence_id.root for item in own),
            stages=tuple(
                EvaluationStageOutcome.of(self._recorded(call.request_id, item.evidence_id))
                for item in own
            ),
        )

    def _recorded(self, request_id: RequestId, evidence_id: EvidenceId) -> TrustedEvidence:
        """The accepted record core's evidence names, from the ledger that holds what it measured."""
        entry = self._evidence.lookup(
            EvidenceKey(source_request=request_id, evidence_id=evidence_id)
        )
        if entry is None:
            message = "core accepted evidence the evidence ledger never recorded"
            raise ContractError(("evidence",), message)
        return entry.evidence

    @staticmethod
    def _call_of(state: CoreState, call_id: str) -> AgentCall:
        """Core's record of the call, which it made when it stepped the event."""
        evaluation = state.evaluation
        call = next((c for c in evaluation.agent_calls if c.call_id == call_id), None)
        if call is None:
            message = "core neither admitted nor refused the evaluation call"
            raise ContractError(("agent_calls",), message)
        return call

    @staticmethod
    def _charge_of(shell: AdmissionShell, owed: OwedSubmission) -> AgentCall | None:
        """The call core answered with a submission under this entry, or None when it holds none."""
        call = next(
            (c for c in shell.admitted_core.evaluation.agent_calls if c.call_id == owed.call_id),
            None,
        )
        return None if call is None or call.request_id is None else call

    def _acknowledger(self, owed: OwedSubmission) -> Callable[[], None]:
        def acknowledge() -> None:
            self._owed.clear(owed.key, owed.call_id)

        return acknowledge

    def _admit(self, shell: AdmissionShell, event: CoreEvent, now: float) -> Transition:
        """Ask core about the event; only the shell's busy refusal (``AdmissionBusyError``) is transient.

        Every other failure is run state that cannot be trusted (a halted shell, an
        uncertain commit, a core contract gap) or a core invariant, and must reach the
        run's halt instead of telling the agent to retry.
        """
        try:
            return shell.admit(event, now_at=now)
        except AdmissionBusyError as error:
            self._diagnostics(
                f"agent evaluation call refused as busy: {type(error).__name__}: {error}"
            )
            raise ToolRefusedError(ToolRefusal.BUSY) from error

    def _scoped(self, scope: Scope) -> _Scoped:
        """The scope's record, joined to the turn executing in it now."""
        scoped = self._scopes.setdefault(scope.model_dump_json(), _Scoped())
        turn = self._executing(scope)
        scoped.begin(None if turn is None else turn.invocation_id)
        return scoped

    def _executing(self, scope: Scope) -> InvocationRef | None:
        """The turn running in ``scope`` per the committed state; None when none or several."""
        shell, _ = self._bound()
        running = [
            row.invocation
            for row in shell.record.envelope.core.sessions.invocations
            if row.scope == scope and row.phase == SessionPhase.EXECUTING
        ]
        return running[0] if len(running) == 1 else None

    @staticmethod
    async def _revision(workspace: SnapshotWorkspace, scoped: _Scoped) -> str:
        """The workspace's content as a revision, reusing the last when nothing changed.

        Identity includes the revision, so a snapshot that is not content-addressed
        would give every call a fresh identity and its own budget.
        """
        last = scoped.last_revision
        try:
            if last is not None and await workspace.matches_revision(last):
                return last
            revision = await workspace.snapshot_and_retain(
                _SNAPSHOT_LABEL, retention_label=_SNAPSHOT_LABEL
            )
        except NestedRepositoryError as error:
            raise ToolRefusedError(ToolRefusal.NESTED_REPOSITORY) from error
        scoped.last_revision = revision
        return revision

    @staticmethod
    def _refusal(call: AgentCall) -> RunStoppingReply:
        if call.rejection is None:
            message = "core neither admitted nor refused the evaluation call"
            raise ContractError(("agent_calls",), message)
        if call.rejection is AgentRejection.RUN_STOPPING:
            return RunStoppingReply()
        raise ToolRefusedError(_REFUSALS[call.rejection])

    # wait and yield

    def _validate_wait(self, scope: Scope, handles: tuple[str, ...]) -> WaitReply:
        shell, _ = self._bound()
        turn = self._executing(scope)
        if turn is None:
            raise ToolRefusedError(ToolRefusal.TURN_ENDED)
        scoped = self._scoped(scope)
        unknown = sorted(set(handles) - scoped.submitted.keys())
        if unknown:
            raise ToolRefusedError(ToolRefusal.UNKNOWN_HANDLES, unknown)
        waits = tuple(dict.fromkeys(handles))
        refusal = suspension_refusal(
            shell.admitted_core,
            turn,
            _owned_wait(shell.admitted_core, turn, waits, scoped.submitted),
        )
        if refusal is not None:
            raise ToolRefusedError(_WAIT_REFUSALS[refusal], waits)
        scoped.waits = waits
        return WaitReply(handles=scoped.waits)

    def yielded(self, request: DispatchTurn) -> Continuation | None:
        """The continuation of a turn whose agent validated a wait, once; else None.

        The turn is over either way: its submissions and wait are forgotten here, so no
        later turn can wait on them.
        """
        scoped = self._scopes.get(request.scope.model_dump_json())
        turn = request.turn
        if scoped is None or scoped.turn != turn.invocation_id:
            return None
        handles, deadlines = scoped.waits, scoped.submitted
        scoped.begin(None)
        if not handles:
            return None
        current = InvocationRef(
            session_id=turn.session.session_id,
            invocation_id=turn.invocation_id,
            generation=request.scope.generation,
        )
        shell, _ = self._bound()
        wait = _continuation(current, handles, deadlines)
        if (
            suspension_refusal(
                shell.admitted_core,
                current,
                _owned_wait(shell.admitted_core, current, handles, deadlines),
            )
            is not None
        ):
            # The scope changed since the wait was validated: end the turn without
            # suspending, as after a host restart. The measurements report ordinarily.
            return None
        return wait

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
    "AgentEvaluationRecords",
    "AgentWorkspaces",
    "ScopeWorkspaces",
    "SnapshotWorkspace",
]
